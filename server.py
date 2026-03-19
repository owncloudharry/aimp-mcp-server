"""
AIMP MCP Server
===============
Steuert den AIMP Music Player über das Model Context Protocol (MCP).
Basiert auf pyaimp (Windows Remote API via Win32 Messages).

Voraussetzungen:
    pip install mcp pyaimp pywin32 mutagen

Konfiguration per MCP-JSON (empfohlen, kein Anfassen der server.py nötig):
    "env": {
        "AIMP_EXE":      "C:\\Program Files\\AIMP\\AIMP.exe",
        "AIMP_MUSIC_DIRS": "D:\\Musik;E:\\Mehr Musik",
        "AIMP_PLAYLIST_DIR": "D:\\Musik\\Playlisten"
    }

Mehrere Musikordner werden mit Semikolon getrennt: "D:\\Musik;E:\\Musik2"
"""

import json
import os
import struct
import subprocess
import time
import unicodedata
import datetime as _dt
from pathlib import Path
from typing import List, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# ── Konfiguration (aus Umgebungsvariablen, gesetzt im MCP-JSON-Eintrag) ───────

# Fallback-Werte — werden durch env-Einträge im MCP-JSON überschrieben
_DEFAULT_AIMP_EXE      = r"C:\Program Files\AIMP\AIMP.exe"
_DEFAULT_MUSIC_DIRS    = r"D:\OneDrive\Musik"
_DEFAULT_PLAYLIST_DIR  = r"D:\OneDrive\Musik\Playlisten"

AIMP_EXE:      str       = os.environ.get("AIMP_EXE",          _DEFAULT_AIMP_EXE)
PLAYLIST_DIR:  str       = os.environ.get("AIMP_PLAYLIST_DIR", _DEFAULT_PLAYLIST_DIR)
MUSIC_DIRS:    List[str] = os.environ.get("AIMP_MUSIC_DIRS",   _DEFAULT_MUSIC_DIRS).split(";")

# Pfad zur AIMP AudioLibrary Datenbank (für Statistiken)
_DEFAULT_ADB_PATH = r"C:\Users\Administrator\AppData\Roaming\AIMP\AudioLibrary\Local.adb"
ADB_PATH: str = os.environ.get("AIMP_ADB_PATH", _DEFAULT_ADB_PATH)

# Sekunden warten bis AIMP nach dem Start antwortet
AIMP_START_WAIT: int = 4

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav",
                    ".wma", ".opus", ".ape", ".mpc", ".mp4"}

MAX_SEARCH_RESULTS = 50    # Maximale Treffer pro Suche
MAX_SCAN_FILES     = 50000 # Sicherheitslimit beim Scannen

# mutagen für ID3-Tag-Lesen (Jahr, Genre etc.) — optional, Fallback auf Dateiname
try:
    from mutagen import File as MutagenFile
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

# ── Server-Instanz ────────────────────────────────────────────────────────────

mcp = FastMCP("aimp_mcp")

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _get_client():
    """Gibt einen pyaimp-Client zurück.

    Falls AIMP nicht läuft, wird es automatisch gestartet und auf das
    Fenster gewartet. Schlägt der Start fehl, wird eine klare Exception geworfen.
    """
    try:
        import pyaimp
    except ImportError:
        raise RuntimeError(
            "pyaimp ist nicht installiert. Bitte ausführen: pip install pyaimp pywin32"
        )

    # Erster Versuch: AIMP läuft bereits
    try:
        return pyaimp.Client()
    except RuntimeError:
        pass

    # AIMP nicht gefunden → automatisch starten
    aimp_path = Path(AIMP_EXE)
    if not aimp_path.exists():
        raise RuntimeError(
            f"AIMP läuft nicht und wurde nicht gefunden unter: {AIMP_EXE}\n"
            "Bitte AIMP_EXE in server.py anpassen."
        )

    # cmd /c start startet AIMP als echten unabhängigen Prozess.
    # "start" in cmd.exe erzeugt immer einen neuen unabhängigen Prozessbaum —
    # zuverlässiger als subprocess direkt, keine PowerShell-Policy-Probleme.
    subprocess.Popen(
        ["cmd", "/c", "start", "", str(aimp_path)],
        creationflags=0x08000000,  # CREATE_NO_WINDOW
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    # Warten bis AIMP-Fenster bereit ist (mit Timeout)
    deadline = time.time() + AIMP_START_WAIT + 10
    last_error = None
    while time.time() < deadline:
        time.sleep(1)
        try:
            return pyaimp.Client()
        except RuntimeError as e:
            last_error = e

    raise RuntimeError(
        f"AIMP wurde gestartet, antwortet aber nicht nach {AIMP_START_WAIT + 10}s. "
        f"Letzter Fehler: {last_error}"
    )


def _format_duration(ms: int) -> str:
    """Konvertiert Millisekunden in lesbares Format mm:ss."""
    if not ms:
        return "0:00"
    seconds = ms // 1000
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}:{seconds:02d}"


def _track_info_to_dict(client) -> dict:
    """Liest den aktuellen Track-Info als Dict aus."""
    try:
        info     = client.get_current_track_info()
        duration = info.get("duration", 0)
        position = client.get_player_position()
        return {
            "title":       info.get("title", "Unbekannt"),
            "artist":      info.get("artist", "Unbekannt"),
            "album":       info.get("album", ""),
            "genre":       info.get("genre", ""),
            "year":        info.get("year", ""),
            "filename":    info.get("filename", ""),
            "duration":    _format_duration(duration),
            "position":    _format_duration(position),
            "duration_ms": duration,
            "position_ms": position,
        }
    except Exception as e:
        return {"error": str(e)}


def _playback_state_label(client) -> str:
    """Gibt den Wiedergabe-Status als lesbaren String zurück."""
    try:
        import pyaimp
        state = client.get_playback_state()
        return {
            pyaimp.PlayBackState.Stopped: "stopped",
            pyaimp.PlayBackState.Playing: "playing",
            pyaimp.PlayBackState.Paused:  "paused",
        }.get(state, "unknown")
    except Exception:
        return "unknown"


def _normalize(text: str) -> str:
    """Normalisiert Text für toleranten Vergleich (lowercase, ohne Akzente)."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def _scan_music_files(dirs: List[str]) -> List[Path]:
    """Scannt Ordner rekursiv nach Audio-Dateien."""
    found: List[Path] = []
    for base in dirs:
        p = Path(base)
        if not p.exists():
            continue
        for f in p.rglob("*"):
            if f.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(f)
                if len(found) >= MAX_SCAN_FILES:
                    return found
    return found


def _read_tags(f: Path) -> dict:
    """Liest ID3/Vorbis-Tags via mutagen. Gibt leeres Dict zurück falls nicht verfügbar."""
    if not MUTAGEN_AVAILABLE:
        return {}
    try:
        audio = MutagenFile(f, easy=True)
        if not audio:
            return {}
        def _tag(key):
            val = audio.tags.get(key) if audio.tags else None
            return str(val[0]).strip() if val else ""
        return {
            "title":  _tag("title"),
            "artist": _tag("artist"),
            "album":  _tag("album"),
            "year":   _tag("date")[:4] if _tag("date") else "",
            "genre":  _tag("genre"),
        }
    except Exception:
        return {}


def _search_files(
    files:  List[Path],
    title:  Optional[str] = None,
    artist: Optional[str] = None,
    album:  Optional[str] = None,
    year:   Optional[str] = None,
    genre:  Optional[str] = None,
    query:  Optional[str] = None,
) -> List[dict]:
    """
    Durchsucht Audiodateien nach Übereinstimmungen.
    Nutzt ID3-Tags (via mutagen) wenn verfügbar, sonst Dateiname/Ordnerstruktur.
    Jahr und Genre werden nur aus Tags gelesen (mutagen erforderlich).
    """
    results = []
    t_norm  = _normalize(title)  if title  else None
    ar_norm = _normalize(artist) if artist else None
    al_norm = _normalize(album)  if album  else None
    yr_norm = _normalize(year)   if year   else None
    gn_norm = _normalize(genre)  if genre  else None
    q_norm  = _normalize(query)  if query  else None

    for f in files:
        parts     = [_normalize(p) for p in f.parts]
        stem      = _normalize(f.stem)
        full_path = _normalize(str(f))

        # Tags lesen (nur wenn Jahr/Genre gesucht oder mutagen verfügbar)
        tags = _read_tags(f) if (yr_norm or gn_norm or MUTAGEN_AVAILABLE) else {}

        # Felder aus Tags oder Ordnerstruktur
        tag_title  = _normalize(tags.get("title",  ""))
        tag_artist = _normalize(tags.get("artist", ""))
        tag_album  = _normalize(tags.get("album",  ""))
        tag_year   = _normalize(tags.get("year",   ""))
        tag_genre  = _normalize(tags.get("genre",  ""))

        # Matching — Tags haben Vorrang, Fallback auf Pfad
        title_match  = tag_title  or stem
        artist_match = tag_artist or " ".join(parts)
        album_match  = tag_album  or " ".join(parts)

        if t_norm  and t_norm  not in title_match:  continue
        if ar_norm and ar_norm not in artist_match: continue
        if al_norm and al_norm not in album_match:  continue
        if yr_norm and yr_norm not in tag_year:     continue
        if gn_norm and gn_norm not in tag_genre:    continue
        if q_norm  and q_norm  not in full_path and q_norm not in tag_title                    and q_norm not in tag_artist:    continue

        # Rückgabewert: Tags bevorzugt, sonst Ordnerstruktur
        guessed_artist = f.parts[-3] if len(f.parts) >= 3 else ""
        guessed_album  = f.parts[-2] if len(f.parts) >= 2 else ""
        results.append({
            "title":  tags.get("title")  or f.stem,
            "artist": tags.get("artist") or guessed_artist,
            "album":  tags.get("album")  or guessed_album,
            "year":   tags.get("year",  ""),
            "genre":  tags.get("genre", ""),
            "path":   str(f),
        })
        if len(results) >= MAX_SEARCH_RESULTS:
            break

    return results


# ── Input-Modelle ─────────────────────────────────────────────────────────────

class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

class VolumeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    volume: int = Field(..., description="Lautstärke in Prozent (0–100)", ge=0, le=100)

class PositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_ms: int = Field(..., description="Ziel-Position in Millisekunden", ge=0)

class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title:      Optional[str]       = Field(default=None, description="Songtitel (Teilstring, z.B. 'Bohemian')")
    artist:     Optional[str]       = Field(default=None, description="Künstler/Band (z.B. 'Queen')")
    album:      Optional[str]       = Field(default=None, description="Albumname (z.B. 'A Night at the Opera')")
    year:       Optional[str]       = Field(default=None, description="Erscheinungsjahr (z.B. '1991' oder '199' für alle 90er). Benötigt mutagen.")
    genre:      Optional[str]       = Field(default=None, description="Genre (z.B. 'Rock', 'Metal'). Benötigt mutagen.")
    query:      Optional[str]       = Field(default=None, description="Freitextsuche über Titel, Artist, Album, Pfad")
    music_dirs: Optional[List[str]] = Field(default=None, description="Zu durchsuchende Ordner (Standard: MUSIC_DIRS)")

class PlayByInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title:      Optional[str]       = Field(default=None, description="Songtitel (Teilstring)")
    artist:     Optional[str]       = Field(default=None, description="Künstler/Band")
    album:      Optional[str]       = Field(default=None, description="Albumname")
    year:       Optional[str]       = Field(default=None, description="Erscheinungsjahr (z.B. '1991'). Benötigt mutagen.")
    genre:      Optional[str]       = Field(default=None, description="Genre (z.B. 'Rock'). Benötigt mutagen.")
    query:      Optional[str]       = Field(default=None, description="Freitext-Suche über alle Felder")
    music_dirs: Optional[List[str]] = Field(default=None, description="Zu durchsuchende Ordner (Standard: MUSIC_DIRS)")

class PlayFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(..., description="Absoluter Dateipfad zur Audio-Datei")


class CreatePlaylistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Name der Playlist (ohne .m3u), z.B. 'Meine Lieblinge'", min_length=1, max_length=100)
    title:      Optional[str]       = Field(default=None, description="Songs nach Titel filtern")
    artist:     Optional[str]       = Field(default=None, description="Songs nach Künstler/Band filtern")
    album:      Optional[str]       = Field(default=None, description="Songs nach Album filtern")
    year:       Optional[str]       = Field(default=None, description="Songs nach Jahr filtern (z.B. '1991' oder '199' für 90er). Benötigt mutagen.")
    genre:      Optional[str]       = Field(default=None, description="Songs nach Genre filtern (z.B. 'Metal'). Benötigt mutagen.")
    query:      Optional[str]       = Field(default=None, description="Freitextsuche über alle Felder")
    music_dirs: Optional[List[str]] = Field(default=None, description="Zu durchsuchende Ordner")
    play:       bool                = Field(default=True,  description="Playlist nach Erstellung sofort in AIMP laden und abspielen")


class PlaylistNameInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Name der Playlist (ohne .m3u), z.B. 'Meine Lieblinge'")


class ListPlaylistsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlayAlbumInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    album:      str             = Field(..., description="Albumname (Teilstring, z.B. 'Mutter')")
    artist:     Optional[str]   = Field(default=None, description="Künstler zur Eingrenzung (z.B. 'Rammstein')")
    music_dirs: Optional[List[str]] = Field(default=None, description="Zu durchsuchende Ordner")
    shuffle:    bool            = Field(default=False, description="Album in zufälliger Reihenfolge abspielen")


class ExtendPlaylistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name:       str             = Field(..., description="Name der zu erweiternden Playlist (ohne .m3u)")
    title:      Optional[str]   = Field(default=None, description="Songs nach Titel filtern")
    artist:     Optional[str]   = Field(default=None, description="Songs nach Künstler/Band filtern")
    album:      Optional[str]   = Field(default=None, description="Songs nach Album filtern")
    query:      Optional[str]   = Field(default=None, description="Freitextsuche über alle Felder")
    music_dirs: Optional[List[str]] = Field(default=None, description="Zu durchsuchende Ordner")
    avoid_duplicates: bool      = Field(default=True, description="Bereits enthaltene Songs überspringen")


class ShufflePlaylistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Name der Playlist (ohne .m3u)")


class TopTracksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit:      int           = Field(default=10, description="Anzahl der Tracks (1–100)", ge=1, le=100)
    min_plays:  int           = Field(default=1,  description="Mindest-Wiedergabezahl", ge=1)
    since_date: Optional[str] = Field(default=None, description="Nur Tracks gespielt nach diesem Datum (YYYY-MM-DD)")
    play_and_list: bool       = Field(default=False, description="Ersten Treffer sofort abspielen")


# ── Tools: Status & Info ──────────────────────────────────────────────────────

@mcp.tool(
    name="aimp_get_status",
    annotations={"title": "AIMP Status abrufen", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_get_status(params: EmptyInput) -> str:
    """Gibt den vollständigen aktuellen Status von AIMP zurück.

    Liefert: Wiedergabe-Status, aktueller Track, Lautstärke,
    Shuffle- und Repeat-Einstellungen sowie Mute-Status.

    Returns:
        str: JSON mit state, track, volume, shuffle, repeat, muted
    """
    try:
        client = _get_client()
        return json.dumps({
            "state":   _playback_state_label(client),
            "track":   _track_info_to_dict(client),
            "volume":  client.get_volume(),
            "shuffle": client.is_shuffled(),
            "repeat":  client.is_track_repeated(),
            "muted":   client.is_muted(),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_get_current_track",
    annotations={"title": "Aktuellen Track anzeigen", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_get_current_track(params: EmptyInput) -> str:
    """Gibt Informationen zum aktuell spielenden Track zurück.

    Returns:
        str: JSON mit title, artist, album, genre, year, filename, duration, position
    """
    try:
        return json.dumps(_track_info_to_dict(_get_client()), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Tools: Wiedergabe-Steuerung ───────────────────────────────────────────────

@mcp.tool(
    name="aimp_play",
    annotations={"title": "Wiedergabe starten", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_play(params: EmptyInput) -> str:
    """Startet die Wiedergabe in AIMP (oder setzt sie fort, falls pausiert)."""
    try:
        _get_client().play()
        return json.dumps({"status": "ok", "action": "play"})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_pause",
    annotations={"title": "Wiedergabe pausieren", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_pause(params: EmptyInput) -> str:
    """Pausiert die aktuelle Wiedergabe in AIMP."""
    try:
        _get_client().pause()
        return json.dumps({"status": "ok", "action": "pause"})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_stop",
    annotations={"title": "Wiedergabe stoppen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_stop(params: EmptyInput) -> str:
    """Stoppt die Wiedergabe in AIMP vollständig."""
    try:
        _get_client().stop()
        return json.dumps({"status": "ok", "action": "stop"})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_next_track",
    annotations={"title": "Nächster Track", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_next_track(params: EmptyInput) -> str:
    """Springt zum nächsten Track in der Playlist."""
    try:
        client = _get_client()
        client.next()
        return json.dumps({"status": "ok", "action": "next",
                           "track": _track_info_to_dict(client)}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_previous_track",
    annotations={"title": "Vorheriger Track", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_previous_track(params: EmptyInput) -> str:
    """Springt zum vorherigen Track in der Playlist."""
    try:
        client = _get_client()
        client.prev()
        return json.dumps({"status": "ok", "action": "previous",
                           "track": _track_info_to_dict(client)}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Tools: Lautstärke & Mute ──────────────────────────────────────────────────

@mcp.tool(
    name="aimp_set_volume",
    annotations={"title": "Lautstärke setzen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_set_volume(params: VolumeInput) -> str:
    """Setzt die Lautstärke von AIMP auf einen bestimmten Wert (0–100).

    Args:
        params (VolumeInput):
            - volume (int): Lautstärke in Prozent (0–100)
    """
    try:
        _get_client().set_volume(params.volume)
        return json.dumps({"status": "ok", "volume": params.volume})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_get_volume",
    annotations={"title": "Lautstärke abrufen", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_get_volume(params: EmptyInput) -> str:
    """Gibt die aktuelle Lautstärke und den Mute-Status zurück."""
    try:
        client = _get_client()
        return json.dumps({"volume": client.get_volume(), "muted": client.is_muted()})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_mute_toggle",
    annotations={"title": "Mute umschalten", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_mute_toggle(params: EmptyInput) -> str:
    """Schaltet den Mute-Status in AIMP um (an/aus)."""
    try:
        client  = _get_client()
        current = client.is_muted()
        client.set_muted(not current)
        return json.dumps({"status": "ok", "muted": not current})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Tools: Shuffle & Repeat ───────────────────────────────────────────────────

@mcp.tool(
    name="aimp_set_shuffle",
    annotations={"title": "Shuffle ein-/ausschalten", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_set_shuffle(params: EmptyInput) -> str:
    """Schaltet den Shuffle-Modus in AIMP um."""
    try:
        client  = _get_client()
        current = client.is_shuffled()
        client.set_shuffled(not current)
        return json.dumps({"status": "ok", "shuffle": not current})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_set_repeat",
    annotations={"title": "Repeat ein-/ausschalten", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_set_repeat(params: EmptyInput) -> str:
    """Schaltet den Repeat-Modus in AIMP um."""
    try:
        client  = _get_client()
        current = client.is_track_repeated()
        client.set_track_repeated(not current)
        return json.dumps({"status": "ok", "repeat": not current})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Tools: Position ───────────────────────────────────────────────────────────

@mcp.tool(
    name="aimp_seek",
    annotations={"title": "Zu Position springen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_seek(params: PositionInput) -> str:
    """Springt zu einer bestimmten Position im aktuellen Track.

    Args:
        params (PositionInput):
            - position_ms (int): Ziel-Position in Millisekunden (z.B. 90000 = 1:30)
    """
    try:
        _get_client().set_player_position(params.position_ms)
        return json.dumps({
            "status":      "ok",
            "position_ms": params.position_ms,
            "position":    _format_duration(params.position_ms),
        })
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Tools: Suche & Datei-Abspielen ────────────────────────────────────────────

@mcp.tool(
    name="aimp_search",
    annotations={"title": "Musikbibliothek durchsuchen", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_search(params: SearchInput) -> str:
    """Durchsucht die lokale Musikbibliothek nach Audio-Dateien.

    Die Suche basiert auf Dateinamen und Ordnerstruktur, z.B.:
        Musik/Queen/A Night at the Opera/Bohemian Rhapsody.mp3

    Mindestens eines der Felder (title, artist, album, query) muss angegeben werden.

    Args:
        params (SearchInput):
            - title      (str, optional): Songtitel-Suche (Teilstring)
            - artist     (str, optional): Künstler/Band
            - album      (str, optional): Albumname
            - query      (str, optional): Freitext über alle Felder
            - music_dirs (list, optional): Zu durchsuchende Ordner

    Returns:
        str: JSON mit found_count und results-Liste (title, artist, album, path)
    """
    if not any([params.title, params.artist, params.album, params.query]):
        return json.dumps({
            "error": "Mindestens ein Suchkriterium angeben: title, artist, album oder query"
        })
    try:
        dirs  = params.music_dirs or MUSIC_DIRS
        files = _scan_music_files(dirs)
        hits  = _search_files(files, params.title, params.artist, params.album,
                              getattr(params, "year", None), getattr(params, "genre", None),
                              params.query)
        return json.dumps({
            "found_count":   len(hits),
            "results":       hits,
            "searched_dirs": dirs,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_search_and_play",
    annotations={"title": "Song suchen und abspielen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_search_and_play(params: PlayByInput) -> str:
    """Sucht einen Song in der Musikbibliothek und spielt ihn sofort in AIMP ab.

    Der erste Treffer wird automatisch abgespielt. Bei mehreren Treffern
    erst aimp_search nutzen, dann aimp_play_file für gezielten Zugriff.

    Mindestens eines der Felder (title, artist, album, query) muss angegeben werden.

    Beispiele:
        title="Bohemian Rhapsody"
        artist="Queen"
        artist="Rammstein", album="Mutter"
        query="Highway to Hell"

    Args:
        params (PlayByInput):
            - title      (str, optional): Songtitel (Teilstring)
            - artist     (str, optional): Künstler/Band
            - album      (str, optional): Albumname
            - query      (str, optional): Freitextsuche
            - music_dirs (list, optional): Zu durchsuchende Ordner

    Returns:
        str: JSON mit status, playing (Treffer-Info) und other_matches (Anzahl weiterer Treffer)
    """
    if not any([params.title, params.artist, params.album, params.query]):
        return json.dumps({
            "error": "Mindestens ein Suchkriterium angeben: title, artist, album oder query"
        })
    try:
        dirs  = params.music_dirs or MUSIC_DIRS
        files = _scan_music_files(dirs)
        hits  = _search_files(files, params.title, params.artist, params.album,
                              getattr(params, "year", None), getattr(params, "genre", None),
                              params.query)

        if not hits:
            return json.dumps({
                "status":  "not_found",
                "message": "Kein passender Song gefunden.",
                "tip":     (
                    "Prüfe mit aimp_list_music_dirs ob die richtigen Ordner konfiguriert sind, "
                    "oder übergib music_dirs direkt als Parameter."
                ),
                "searched_dirs": dirs,
            }, ensure_ascii=False)

        best   = hits[0]
        client = _get_client()
        client.add_to_playlist_and_play(best["path"])

        return json.dumps({
            "status":        "ok",
            "action":        "play",
            "playing":       best,
            "other_matches": len(hits) - 1,
            "tip": (
                f"Noch {len(hits)-1} weiterer Treffer gefunden. "
                "Nutze aimp_search + aimp_play_file für gezielten Zugriff."
            ) if len(hits) > 1 else None,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_play_file",
    annotations={"title": "Datei direkt abspielen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_play_file(params: PlayFileInput) -> str:
    """Spielt eine Audio-Datei direkt über ihren Dateipfad in AIMP ab.

    Ideal in Kombination mit aimp_search: erst suchen, dann einen
    konkreten Treffer per Pfad abspielen.

    Args:
        params (PlayFileInput):
            - path (str): Absoluter Pfad zur Audio-Datei

    Returns:
        str: JSON mit status, path und title
    """
    try:
        path = Path(params.path)
        if not path.exists():
            return json.dumps({"error": f"Datei nicht gefunden: {params.path}"})
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            return json.dumps({"error": f"Kein unterstütztes Audioformat: {path.suffix}"})
        _get_client().add_to_playlist_and_play(str(path))
        return json.dumps({"status": "ok", "action": "play",
                           "path": str(path), "title": path.stem}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_list_music_dirs",
    annotations={"title": "Konfigurierte Musikordner anzeigen", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_list_music_dirs(params: EmptyInput) -> str:
    """Zeigt die aktuell konfigurierten Suchordner und ob sie existieren.

    Returns:
        str: JSON mit music_dirs-Liste (path + exists)
    """
    dirs_info = [{"path": d, "exists": Path(d).exists()} for d in MUSIC_DIRS]
    return json.dumps({
        "music_dirs": dirs_info,
        "tip": (
            "Passe MUSIC_DIRS in server.py an oder übergib music_dirs "
            "direkt bei aimp_search / aimp_search_and_play."
        ),
    }, ensure_ascii=False, indent=2)


# ── Tools: Playlist-Verwaltung ───────────────────────────────────────────────

@mcp.tool(
    name="aimp_create_playlist",
    annotations={"title": "Playlist erstellen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_create_playlist(params: CreatePlaylistInput) -> str:
    """Erstellt eine .m3u Playlist aus gefundenen Songs und lädt sie optional in AIMP.

    Sucht Songs nach den angegebenen Kriterien und speichert die Ergebnisse
    als .m3u Datei im PLAYLIST_DIR. Optional wird die Playlist sofort in AIMP
    geladen und abgespielt.

    Beispiele:
        name="Rock Klassiker", artist="AC/DC"
        name="Rammstein Mix", artist="Rammstein"
        name="Lieblinge", query="best of"

    Args:
        params (CreatePlaylistInput):
            - name       (str):           Playlist-Name (ohne .m3u)
            - title      (str, optional): Titelfilter
            - artist     (str, optional): Künstlerfilter
            - album      (str, optional): Albumfilter
            - query      (str, optional): Freitextfilter
            - music_dirs (list, optional): Zu durchsuchende Ordner
            - play       (bool):          Sofort abspielen (Standard: True)

    Returns:
        str: JSON mit status, playlist_path, track_count und optionalem play-Status
    """
    if not any([params.title, params.artist, params.album, params.query]):
        return json.dumps({
            "error": "Mindestens ein Suchkriterium angeben: title, artist, album oder query"
        })
    try:
        # Songs suchen
        dirs  = params.music_dirs or MUSIC_DIRS
        files = _scan_music_files(dirs)
        hits  = _search_files(files, params.title, params.artist, params.album,
                              getattr(params, "year", None), getattr(params, "genre", None),
                              params.query)

        if not hits:
            return json.dumps({
                "status":  "not_found",
                "message": "Keine Songs gefunden — Playlist wurde nicht erstellt.",
                "searched_dirs": dirs,
            }, ensure_ascii=False)

        # Playlist-Ordner anlegen falls nötig
        playlist_dir = Path(PLAYLIST_DIR)
        playlist_dir.mkdir(parents=True, exist_ok=True)

        # .m3u Datei schreiben
        safe_name    = "".join(c for c in params.name if c not in r'\/:*?"<>|')
        playlist_path = playlist_dir / f"{safe_name}.m3u"

        with open(playlist_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for hit in hits:
                title  = hit.get("title", "")
                artist = hit.get("artist", "")
                f.write(f"#EXTINF:-1,{artist} - {title}\n")
                f.write(f"{hit['path']}\n")

        result = {
            "status":        "ok",
            "playlist_path": str(playlist_path),
            "playlist_name": params.name,
            "track_count":   len(hits),
            "tracks":        [{"title": h["title"], "artist": h["artist"]} for h in hits[:10]],
            "tracks_note":   f"(erste 10 von {len(hits)})" if len(hits) > 10 else None,
        }

        # Optional sofort in AIMP laden
        if params.play:
            client = _get_client()
            client.add_to_playlist_and_play(str(playlist_path))
            result["playing"] = True

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_list_playlists",
    annotations={"title": "Gespeicherte Playlisten anzeigen", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_list_playlists(params: EmptyInput) -> str:
    """Listet alle gespeicherten .m3u Playlisten im PLAYLIST_DIR auf.

    Returns:
        str: JSON mit Liste der Playlisten (name, path, track_count)
    """
    try:
        playlist_dir = Path(PLAYLIST_DIR)
        if not playlist_dir.exists():
            return json.dumps({
                "playlists": [],
                "message": f"Playlist-Ordner existiert noch nicht: {PLAYLIST_DIR}",
                "tip": "Erstelle zuerst eine Playlist mit aimp_create_playlist.",
            }, ensure_ascii=False, indent=2)

        playlists = []
        for f in sorted(playlist_dir.glob("*.m3u")):
            # Tracks zählen (Zeilen ohne # und ohne Leerzeilen)
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
                track_count = sum(1 for l in lines if l.strip() and not l.startswith("#"))
            except Exception:
                track_count = -1
            playlists.append({
                "name":        f.stem,
                "path":        str(f),
                "track_count": track_count,
            })

        return json.dumps({
            "playlist_count": len(playlists),
            "playlists":      playlists,
            "playlist_dir":   str(playlist_dir),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_play_playlist",
    annotations={"title": "Gespeicherte Playlist abspielen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_play_playlist(params: PlaylistNameInput) -> str:
    """Lädt eine gespeicherte Playlist in AIMP und spielt sie ab.

    Args:
        params (PlaylistNameInput):
            - name (str): Playlist-Name (ohne .m3u), z.B. 'Meine Lieblinge'

    Returns:
        str: JSON mit status und Playlist-Info
    """
    try:
        playlist_dir  = Path(PLAYLIST_DIR)
        safe_name     = "".join(c for c in params.name if c not in r'\/:*?"<>|')
        playlist_path = playlist_dir / f"{safe_name}.m3u"

        if not playlist_path.exists():
            # Ähnliche Playlisten vorschlagen
            existing = [f.stem for f in playlist_dir.glob("*.m3u")] if playlist_dir.exists() else []
            return json.dumps({
                "error":     f"Playlist nicht gefunden: {params.name}",
                "available": existing,
                "tip":       "Nutze aimp_list_playlists um alle verfügbaren Playlisten zu sehen.",
            }, ensure_ascii=False)

        client = _get_client()
        client.add_to_playlist_and_play(str(playlist_path))

        # Track-Anzahl lesen
        lines = playlist_path.read_text(encoding="utf-8").splitlines()
        track_count = sum(1 for l in lines if l.strip() and not l.startswith("#"))

        return json.dumps({
            "status":        "ok",
            "action":        "play",
            "playlist_name": params.name,
            "playlist_path": str(playlist_path),
            "track_count":   track_count,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_delete_playlist",
    annotations={"title": "Playlist löschen", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_delete_playlist(params: PlaylistNameInput) -> str:
    """Löscht eine gespeicherte .m3u Playlist.

    Args:
        params (PlaylistNameInput):
            - name (str): Playlist-Name (ohne .m3u)

    Returns:
        str: JSON mit status und gelöschtem Pfad
    """
    try:
        playlist_dir  = Path(PLAYLIST_DIR)
        safe_name     = "".join(c for c in params.name if c not in r'\/:*?"<>|')
        playlist_path = playlist_dir / f"{safe_name}.m3u"

        if not playlist_path.exists():
            return json.dumps({"error": f"Playlist nicht gefunden: {params.name}"})

        playlist_path.unlink()
        return json.dumps({
            "status":  "ok",
            "action":  "deleted",
            "name":    params.name,
            "path":    str(playlist_path),
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Tools: Album & Playlist-Erweiterung ──────────────────────────────────────

@mcp.tool(
    name="aimp_play_album",
    annotations={"title": "Album komplett abspielen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_play_album(params: PlayAlbumInput) -> str:
    """Sucht ein Album und spielt alle Tracks in richtiger Reihenfolge ab.

    Die Tracks werden nach Dateiname sortiert (typisch: 01_Track.mp3, 02_Track.mp3).
    Optional kann das Album auch in zufälliger Reihenfolge abgespielt werden.

    Beispiele:
        album="Mutter", artist="Rammstein"
        album="Back in Black"
        album="The Dark Side of the Moon", shuffle=True

    Args:
        params (PlayAlbumInput):
            - album      (str):           Albumname (Teilstring)
            - artist     (str, optional): Künstler zur Eingrenzung
            - music_dirs (list, optional): Zu durchsuchende Ordner
            - shuffle    (bool):          Zufällige Reihenfolge (Standard: False)

    Returns:
        str: JSON mit status, album_info und track_count
    """
    try:
        dirs  = params.music_dirs or MUSIC_DIRS
        files = _scan_music_files(dirs)
        hits  = _search_files(files, title=None, artist=params.artist,
                              album=params.album, year=None, genre=None, query=None)

        if not hits:
            return json.dumps({
                "status":  "not_found",
                "message": f"Album nicht gefunden: {params.album}",
                "tip":     "Prüfe den Albumnamen mit aimp_search (album=...) um verfügbare Alben zu sehen.",
            }, ensure_ascii=False)

        # Tracks sortieren (nach Dateiname = Tracknummer)
        hits_sorted = sorted(hits, key=lambda h: _normalize(Path(h["path"]).name))

        if params.shuffle:
            import random
            random.shuffle(hits_sorted)

        # Temporäre Playlist erstellen und abspielen
        playlist_dir = Path(PLAYLIST_DIR)
        playlist_dir.mkdir(parents=True, exist_ok=True)

        album_name  = hits_sorted[0]["album"] if hits_sorted[0]["album"] else params.album
        artist_name = hits_sorted[0]["artist"] if hits_sorted[0]["artist"] else (params.artist or "")
        safe_name   = "".join(c for c in f"Album - {artist_name} - {album_name}"
                              if c not in r'\/:*?"<>|')
        playlist_path = playlist_dir / f"{safe_name}.m3u"

        with open(playlist_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for hit in hits_sorted:
                f.write(f"#EXTINF:-1,{hit['artist']} - {hit['title']}\n")
                f.write(f"{hit['path']}\n")

        client = _get_client()
        client.add_to_playlist_and_play(str(playlist_path))

        return json.dumps({
            "status":      "ok",
            "action":      "play",
            "album":       album_name,
            "artist":      artist_name,
            "track_count": len(hits_sorted),
            "shuffled":    params.shuffle,
            "playlist":    str(playlist_path),
            "tracks":      [{"track": i+1, "title": h["title"]}
                            for i, h in enumerate(hits_sorted)],
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_extend_playlist",
    annotations={"title": "Playlist erweitern", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_extend_playlist(params: ExtendPlaylistInput) -> str:
    """Fügt Songs zu einer bestehenden Playlist hinzu.

    Sucht Songs nach den angegebenen Kriterien und hängt sie an die
    bestehende Playlist an. Duplikate werden standardmäßig übersprungen.

    Beispiele:
        name="Rock Klassiker", artist="AC/DC"    → AC/DC Songs hinzufügen
        name="Meine Playlist", album="Mutter"    → ganzes Album hinzufügen
        name="Mix", query="live"                 → alle Live-Tracks hinzufügen

    Args:
        params (ExtendPlaylistInput):
            - name            (str):           Name der bestehenden Playlist
            - title           (str, optional): Titelfilter
            - artist          (str, optional): Künstlerfilter
            - album           (str, optional): Albumfilter
            - query           (str, optional): Freitextfilter
            - music_dirs      (list, optional): Zu durchsuchende Ordner
            - avoid_duplicates (bool):         Duplikate überspringen (Standard: True)

    Returns:
        str: JSON mit status, added_count, skipped_count, total_count
    """
    if not any([params.title, params.artist, params.album, params.query]):
        return json.dumps({
            "error": "Mindestens ein Suchkriterium angeben: title, artist, album oder query"
        })
    try:
        playlist_dir  = Path(PLAYLIST_DIR)
        safe_name     = "".join(c for c in params.name if c not in r'\/:*?"<>|')
        playlist_path = playlist_dir / f"{safe_name}.m3u"

        if not playlist_path.exists():
            existing = [f.stem for f in playlist_dir.glob("*.m3u")] if playlist_dir.exists() else []
            return json.dumps({
                "error":     f"Playlist nicht gefunden: {params.name}",
                "available": existing,
                "tip":       "Nutze aimp_list_playlists oder aimp_create_playlist.",
            }, ensure_ascii=False)

        # Bestehende Pfade einlesen (für Duplikat-Check)
        existing_content = playlist_path.read_text(encoding="utf-8")
        existing_paths   = set(
            l.strip() for l in existing_content.splitlines()
            if l.strip() and not l.startswith("#")
        )

        # Neue Songs suchen
        dirs  = params.music_dirs or MUSIC_DIRS
        files = _scan_music_files(dirs)
        hits  = _search_files(files, params.title, params.artist, params.album,
                              getattr(params, "year", None), getattr(params, "genre", None),
                              params.query)

        added   = []
        skipped = 0
        with open(playlist_path, "a", encoding="utf-8") as f:
            for hit in hits:
                if params.avoid_duplicates and hit["path"] in existing_paths:
                    skipped += 1
                    continue
                f.write(f"#EXTINF:-1,{hit['artist']} - {hit['title']}\n")
                f.write(f"{hit['path']}\n")
                existing_paths.add(hit["path"])
                added.append({"title": hit["title"], "artist": hit["artist"]})

        # Neue Gesamtzahl zählen
        new_content = playlist_path.read_text(encoding="utf-8")
        total = sum(1 for l in new_content.splitlines() if l.strip() and not l.startswith("#"))

        return json.dumps({
            "status":        "ok",
            "playlist_name": params.name,
            "added_count":   len(added),
            "skipped_count": skipped,
            "total_count":   total,
            "added_tracks":  added[:10],
            "added_note":    f"(erste 10 von {len(added)})" if len(added) > 10 else None,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_shuffle_playlist",
    annotations={"title": "Playlist mischen", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_shuffle_playlist(params: ShufflePlaylistInput) -> str:
    """Mischt eine bestehende Playlist in zufälliger Reihenfolge und spielt sie ab.

    Die originale .m3u Datei wird dabei neu geschrieben (gemischte Reihenfolge).

    Args:
        params (ShufflePlaylistInput):
            - name (str): Name der Playlist (ohne .m3u)

    Returns:
        str: JSON mit status, track_count und neuer Reihenfolge (erste 10)
    """
    import random
    try:
        playlist_dir  = Path(PLAYLIST_DIR)
        safe_name     = "".join(c for c in params.name if c not in r'\/:*?"<>|')
        playlist_path = playlist_dir / f"{safe_name}.m3u"

        if not playlist_path.exists():
            existing = [f.stem for f in playlist_dir.glob("*.m3u")] if playlist_dir.exists() else []
            return json.dumps({
                "error":     f"Playlist nicht gefunden: {params.name}",
                "available": existing,
            }, ensure_ascii=False)

        # Tracks aus M3U lesen (EXTINF + Pfad als Paare)
        lines  = playlist_path.read_text(encoding="utf-8").splitlines()
        tracks = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF"):
                ext_line = line
                i += 1
                if i < len(lines) and lines[i].strip() and not lines[i].startswith("#"):
                    tracks.append((ext_line, lines[i].strip()))
                    i += 1
            elif line and not line.startswith("#"):
                tracks.append(("", line))
                i += 1
            else:
                i += 1

        if not tracks:
            return json.dumps({"error": "Playlist ist leer oder konnte nicht gelesen werden."})

        random.shuffle(tracks)

        # Gemischte Playlist zurückschreiben
        with open(playlist_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for ext, path in tracks:
                if ext:
                    f.write(f"{ext}\n")
                f.write(f"{path}\n")

        # In AIMP laden
        client = _get_client()
        client.add_to_playlist_and_play(str(playlist_path))

        preview = [Path(p).stem for _, p in tracks[:10]]

        return json.dumps({
            "status":      "ok",
            "action":      "shuffled_and_play",
            "playlist":    params.name,
            "track_count": len(tracks),
            "first_10":    preview,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── ADB-Statistiken ──────────────────────────────────────────────────────────

def _read_adb_stats(adb_path: str, limit: int, min_plays: int,
                    since_ts: int = 0) -> list:
    """
    Liest PlaybackCount und LastPlayback direkt aus der AIMP .adb Datenbank.

    Record-Layout (Reverse-Engineering 18.-19.03.2026):
      RECORD_MARKER [00 02 00 06 00 00 00 00]
      ...Pfad (UTF-16)...
      ...Timestamps...
      [02 Count 02 Count]   ← PlaybackCount (pf-4)  wenn gespielt
      [0D 0D 02 00]         ← pf-4  wenn nie gespielt
      [0D 06 00 00]         ← pf (Count-Marker)
    """
    from pathlib import Path as _Path

    adb = _Path(adb_path)
    if not adb.exists():
        return []

    data = adb.read_bytes()

    RECORD_MARKER  = b"\x00\x02\x00\x06\x00\x00\x00\x00"
    PRE_PATH       = b"\x02\x01\x06"
    COUNT_MARKER   = b"\x0d\x06\x00\x00"
    OLE_BASE_DAYS  = 25569.0  # Tage zwischen 1899-12-30 und 1970-01-01

    results = []
    pos = 0

    while True:
        idx = data.find(RECORD_MARKER, pos)
        if idx == -1:
            break
        pos = idx + 1

        # Record-Ende = nächster Marker
        next_idx = data.find(RECORD_MARKER, pos)
        end = min(next_idx, idx + 8192) if next_idx != -1 else idx + 8192

        # Count-Marker finden
        cm = data.find(COUNT_MARKER, idx, end)
        if cm == -1 or cm < 4:
            continue

        # PlaybackCount aus [02 Count 02 Count] bei cm-4
        b = data[cm-4:cm]
        if not (b[0] == 0x02 and b[2] == 0x02 and b[1] == b[3] and b[1] >= min_plays):
            continue
        play_count = b[1]

        # LastPlayback OLE float64 bei cm-12
        last_play_ts = 0
        if cm >= 12:
            ole = struct.unpack_from("<d", data, cm-12)[0]
            if 30000 < ole < 60000:
                last_play_ts = int((ole - OLE_BASE_DAYS) * 86400)

        # Datumsfilter
        if since_ts and last_play_ts < since_ts:
            continue

        # Dateipfad lesen
        mp = data.find(PRE_PATH, idx, idx + 64)
        if mp == -1:
            continue
        char_count = struct.unpack_from("<H", data, mp + 3)[0]
        if char_count == 0 or char_count > 512 or mp + 5 + char_count * 2 > len(data):
            continue
        try:
            path = data[mp + 5: mp + 5 + char_count * 2].decode("utf-16-le", errors="replace")
        except Exception:
            continue

        if not path or len(path) < 4:
            continue

        results.append({
            "path":        path,
            "title":       _Path(path).stem,
            "play_count":  play_count,
            "last_played": (
                _dt.datetime.fromtimestamp(last_play_ts).strftime("%Y-%m-%d %H:%M")
                if last_play_ts else "unbekannt"
            ),
            "last_played_ts": last_play_ts,
        })

    # Sortieren nach PlayCount DESC, dann LastPlayed DESC
    results.sort(key=lambda x: (x["play_count"], x["last_played_ts"]), reverse=True)
    return results[:limit]


# ── Tools: Statistiken ────────────────────────────────────────────────────────

@mcp.tool(
    name="aimp_top_tracks",
    annotations={"title": "Meistgespielte Tracks", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def aimp_top_tracks(params: TopTracksInput) -> str:
    """Gibt die meistgespielten Tracks aus der AIMP-Datenbank zurück.

    Liest PlaybackCount und LastPlayback direkt aus Local.adb.
    Nur Tracks die in dieser AIMP-Installation tatsächlich gespielt wurden
    (nicht alte ID3-Tags oder importierte Zähler).

    Beispiele:
        Top 10 meistgespielte Tracks
        Top 20 Tracks gespielt nach 2026-01-01
        Tracks mit mindestens 5 Plays

    Args:
        params (TopTracksInput):
            - limit      (int):  Anzahl Tracks (Standard: 10, max 100)
            - min_plays  (int):  Mindest-Wiedergabezahl (Standard: 1)
            - since_date (str):  Nur ab Datum YYYY-MM-DD (optional)
            - play_and_list (bool): Ersten Track sofort abspielen

    Returns:
        str: JSON mit ranked_tracks (rank, title, play_count, last_played, path)
    """
    try:
        since_ts = 0
        if params.since_date:
            try:
                since_dt = _dt.datetime.strptime(params.since_date, "%Y-%m-%d")
                since_ts = int(since_dt.timestamp())
            except ValueError:
                return json.dumps({"error": f"Ungültiges Datum: {params.since_date}. Format: YYYY-MM-DD"})

        tracks = _read_adb_stats(ADB_PATH, params.limit, params.min_plays, since_ts)

        if not tracks:
            return json.dumps({
                "status":  "empty",
                "message": "Keine Tracks gefunden.",
                "tip":     f"Prüfe AIMP_ADB_PATH ({ADB_PATH}) im MCP-JSON.",
            }, ensure_ascii=False)

        # Optional ersten Track abspielen
        if params.play_and_list and tracks:
            try:
                client = _get_client()
                client.add_to_playlist_and_play(tracks[0]["path"])
            except Exception:
                pass

        ranked = [
            {
                "rank":        i + 1,
                "title":       t["title"],
                "play_count":  t["play_count"],
                "last_played": t["last_played"],
                "path":        t["path"],
            }
            for i, t in enumerate(tracks)
        ]

        return json.dumps({
            "status":       "ok",
            "total_found":  len(ranked),
            "min_plays":    params.min_plays,
            "since_date":   params.since_date or "alle",
            "tracks":       ranked,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="aimp_track_stats",
    annotations={"title": "Statistiken für einen Track", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def aimp_track_stats(params: PlayFileInput) -> str:
    """Gibt PlaybackCount und LastPlayback für einen bestimmten Track zurück.

    Args:
        params (PlayFileInput):
            - path (str): Absoluter Pfad zur Audio-Datei

    Returns:
        str: JSON mit play_count, last_played, path
    """
    try:
        from pathlib import Path as _Path
        adb = _Path(ADB_PATH)
        if not adb.exists():
            return json.dumps({"error": f"AIMP-Datenbank nicht gefunden: {ADB_PATH}"})

        data = adb.read_bytes()

        # Dateiname in UTF-16 suchen
        search_name = _Path(params.path).name
        needle = search_name.encode("utf-16-le")
        pos = data.find(needle)
        if pos == -1:
            return json.dumps({
                "status":  "not_found",
                "message": f"Track nicht in AIMP-Datenbank: {search_name}",
                "tip":     "Track muss zuerst in AIMP importiert werden.",
            })

        COUNT_MARKER  = b"\x0d\x06\x00\x00"
        RECORD_MARKER = b"\x00\x02\x00\x06\x00\x00\x00\x00"
        OLE_BASE_DAYS = 25569.0

        rec = data.rfind(RECORD_MARKER, max(0, pos - 2048), pos)
        end = data.find(RECORD_MARKER, pos, pos + 4096) or pos + 4096
        cm  = data.find(COUNT_MARKER, rec if rec != -1 else pos - 512, end)

        play_count    = 0
        last_played   = "nie gespielt"
        last_played_ts = 0

        if cm != -1 and cm >= 4:
            b = data[cm-4:cm]
            if b[0] == 0x02 and b[2] == 0x02 and b[1] == b[3]:
                play_count = b[1]
            if play_count > 0 and cm >= 12:
                ole = struct.unpack_from("<d", data, cm-12)[0]
                if 30000 < ole < 60000:
                    last_played_ts = int((ole - OLE_BASE_DAYS) * 86400)
                    last_played = _dt.datetime.fromtimestamp(last_played_ts).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

        return json.dumps({
            "path":        params.path,
            "title":       _Path(params.path).stem,
            "play_count":  play_count,
            "last_played": last_played,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Einstiegspunkt ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
