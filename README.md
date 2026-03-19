# AIMP MCP Server

Steuert den **AIMP Music Player** über das Model Context Protocol (MCP).  
Funktioniert mit **Claude Desktop** und **LM Studio**.

---

## 1. Python & Abhängigkeiten installieren

1. https://www.python.org/downloads/ → Python 3.11+, Haken bei „Add Python to PATH"
2. In einer **cmd.exe**:
   ```
   pip install mcp pyaimp pywin32 mutagen
   ```
   > `mutagen` ist optional — ohne es funktioniert alles außer Jahr- und Genre-Suche.

---

## 2. server.py ablegen

Kopiere `server.py` in einen festen Ordner, z.B.:
```
C:\Users\DEIN_NAME\aimp_mcp\server.py
```

---

## 3. Konfiguration per MCP-JSON (empfohlen)

Alle Pfade werden direkt im MCP-JSON-Eintrag hinterlegt — **kein Anfassen der server.py nötig**.  
Das macht es einfach mehrere Rechner mit unterschiedlichen Pfaden zu betreiben.

### Claude Desktop

Datei öffnen: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "aimp": {
      "command": "python",
      "args": ["C:\\Users\\DEIN_NAME\\aimp_mcp\\server.py"],
      "env": {
        "AIMP_EXE":          "C:\\Program Files\\AIMP\\AIMP.exe",
        "AIMP_MUSIC_DIRS":   "D:\\DEIN_PFAD\Musik",
        "AIMP_PLAYLIST_DIR": "D:\\DEIN_PFAD\\Musik\\Playlisten",
        "AIMP_ADB_PATH":     "C:\\Users\\DEIN_NAME\\AppData\\Roaming\\AIMP\\AudioLibrary\\Local.adb"
      }
    }
  }
}
```

**Mehrere Musikordner** → mit Semikolon trennen:
```json
"AIMP_MUSIC_DIRS": "D:\\Musik;E:\\Mehr Musik;F:\\Archiv"
```

### LM Studio

Einstellungen → MCP Servers → Add Server → stdio:
```json
{
  "command": "python",
  "args": ["C:\\Users\\DEIN_NAME\\aimp_mcp\\server.py"],
  "env": {
    "AIMP_EXE":          "C:\\Program Files\\AIMP\\AIMP.exe",
    "AIMP_MUSIC_DIRS":   "D:\\DEIN_PFAD\\Musik",
    "AIMP_PLAYLIST_DIR": "D:\\DEIN_PFAD\\Musik\\Playlisten"
  }
}
```

---

## 4. Verfügbare Tools & Beispiel-Prompts

| Tool | Beispiel-Prompt |
|---|---|
| `aimp_get_status` | „Was spielt gerade?" |
| `aimp_play/pause/stop` | „Pause", „Play", „Stop" |
| `aimp_next/previous_track` | „Nächster Song", „Zurück" |
| `aimp_set_volume` | „Lautstärke auf 60%" |
| `aimp_mute_toggle` | „Ton aus" |
| `aimp_set_shuffle` | „Shuffle an/aus" |
| `aimp_set_repeat` | „Repeat an/aus" |
| `aimp_seek` | „Springe zu Minute 2:30" |
| `aimp_search` | „Suche alle Songs von Queen" |
| `aimp_search_and_play` | „Spiele Bohemian Rhapsody" |
| `aimp_search_and_play` | „Spiele etwas von Rammstein aus 1997" |
| `aimp_search_and_play` | „Spiele einen Metal-Song" |
| `aimp_play_album` | „Spiele das Album Mutter von Rammstein" |
| `aimp_play_file` | direkter Dateipfad |
| `aimp_create_playlist` | „Erstelle Playlist 90er Rock mit Rock-Songs aus den 90ern" |
| `aimp_list_playlists` | „Zeige meine Playlisten" |
| `aimp_play_playlist` | „Spiele Playlist Rammstein Mix" |
| `aimp_extend_playlist` | „Füge AC/DC Songs zur Playlist Rock hinzu" |
| `aimp_shuffle_playlist` | „Mische die Playlist Rock Mix" |
| `aimp_delete_playlist` | „Lösche Playlist Alt" |
| `aimp_list_music_dirs` | „Welche Musikordner sind konfiguriert?" |

---

## 5. Troubleshooting

**Jahr/Genre-Suche funktioniert nicht**  
→ `pip install mutagen` ausführen, MCP-Client neu starten.

**AIMP startet nicht automatisch**  
→ `AIMP_EXE` Pfad im JSON prüfen. Doppelte Backslashes `\\` in JSON sind Pflicht.

**Keine Songs gefunden**  
→ `AIMP_MUSIC_DIRS` prüfen. Mehrere Ordner mit `;` trennen.

**Pfad-Fehler unter Windows**  
→ In JSON immer doppelte Backslashes: `C:\\Musik` nicht `C:\Musik`.
