# AIMP MCP Server

> Control the [AIMP music player](https://www.aimp.ru/) through natural language via the **Model Context Protocol (MCP)**.  
> Works with **Claude Desktop**, **LM Studio**, and any other MCP-compatible AI client.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey)
![MCP Compatible](https://img.shields.io/badge/MCP-compatible-green)
![License: MIT](https://img.shields.io/badge/license-MIT-yellow)

---

## What can you do?

```
"Play something by Rammstein from 1997"
"What are my 10 most played songs?"
"Create a playlist with 90s rock songs and shuffle it"
"Show all tracks rated 5 stars"
"When did I last listen to Falco?"
```

**28 tools** covering playback, search, playlist management, statistics, and ratings.

### Unique feature: Direct database access

Unlike simple remote-control integrations, this server reads AIMP's **binary AudioLibrary database** (`Local.adb`) directly — no AIMP API needed. This enables:

- **Play counts & last-played timestamps** from AIMP's internal tracking
- **Star ratings** as set inside AIMP (not ID3 POPM tags)
- **Top tracks** with date filters and minimum play count thresholds

The `.adb` format (ADLMEMDB) was reverse-engineered to decode record layout, OLE timestamps, and rating fields.

---

## Requirements

- Windows (AIMP is Windows-only)
- [AIMP](https://www.aimp.ru/) installed
- Python 3.11+
- An MCP-compatible client (Claude Desktop, LM Studio, etc.)

---

## Installation

### 1. Install dependencies

```cmd
pip install mcp pyaimp pywin32 mutagen
```

> `mutagen` is optional — everything works without it except year- and genre-based search.

### 2. Place server.py

Copy `server.py` to a permanent folder, e.g.:
```
C:\Users\YOUR_NAME\aimp_mcp\server.py
```

### 3. Configure your MCP client

All paths are set via environment variables in the MCP config — **no need to edit server.py**.  
This makes it easy to run on multiple machines with different paths.

#### Claude Desktop

Open: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "aimp": {
      "command": "python",
      "args": ["C:\\Users\\YOUR_NAME\\aimp_mcp\\server.py"],
      "env": {
        "AIMP_EXE":          "C:\\Program Files\\AIMP\\AIMP.exe",
        "AIMP_MUSIC_DIRS":   "D:\\Music",
        "AIMP_PLAYLIST_DIR": "D:\\Music\\Playlists",
        "AIMP_ADB_PATH":     "C:\\Users\\YOUR_NAME\\AppData\\Roaming\\AIMP\\AudioLibrary\\Local.adb"
      }
    }
  }
}
```

**Multiple music folders** → separate with semicolons:
```json
"AIMP_MUSIC_DIRS": "D:\\Music;E:\\More Music;F:\\Archive"
```

#### LM Studio

Settings → MCP Servers → Add Server → stdio, then paste:
```json
{
  "command": "python",
  "args": ["C:\\Users\\YOUR_NAME\\aimp_mcp\\server.py"],
  "env": {
    "AIMP_EXE":          "C:\\Program Files\\AIMP\\AIMP.exe",
    "AIMP_MUSIC_DIRS":   "D:\\Music",
    "AIMP_PLAYLIST_DIR": "D:\\Music\\Playlists",
    "AIMP_ADB_PATH":     "C:\\Users\\YOUR_NAME\\AppData\\Roaming\\AIMP\\AudioLibrary\\Local.adb"
  }
}
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AIMP_EXE` | No | Path to AIMP.exe (default: `C:\Program Files\AIMP\AIMP.exe`) |
| `AIMP_MUSIC_DIRS` | No | Music folders, separated by `;` |
| `AIMP_PLAYLIST_DIR` | No | Folder for `.m3u` playlists |
| `AIMP_ADB_PATH` | No* | Path to `Local.adb` — required for statistics and rating tools |

*Without `AIMP_ADB_PATH`, all tools work except `aimp_top_tracks`, `aimp_track_stats`, `aimp_search_by_rating`, and `aimp_get_track_rating`.

---

## Available Tools

### Playback Control

| Tool | Example prompt |
|---|---|
| `aimp_get_status` | "What's playing right now?" |
| `aimp_get_current_track` | "Which song is this?" |
| `aimp_play` | "Play" |
| `aimp_pause` | "Pause" |
| `aimp_stop` | "Stop" |
| `aimp_next_track` | "Next song" |
| `aimp_previous_track` | "Go back" |
| `aimp_set_volume` | "Set volume to 60%" |
| `aimp_get_volume` | "How loud is it?" |
| `aimp_mute_toggle` | "Mute" |
| `aimp_set_shuffle` | "Shuffle on/off" |
| `aimp_set_repeat` | "Repeat on/off" |
| `aimp_seek` | "Jump to 2:30" |

### Search & Play

| Tool | Example prompt |
|---|---|
| `aimp_search` | "Find all songs by Queen" |
| `aimp_search_and_play` | "Play Bohemian Rhapsody" |
| `aimp_search_and_play` | "Play something by Rammstein from 1997" |
| `aimp_search_and_play` | "Play a metal song" |
| `aimp_play_album` | "Play the album Mutter by Rammstein" |
| `aimp_play_file` | "Play D:\Music\song.mp3" |
| `aimp_list_music_dirs` | "Which music folders are configured?" |

### Playlists

| Tool | Example prompt |
|---|---|
| `aimp_create_playlist` | "Create a playlist called 90s Rock with rock songs from the 90s" |
| `aimp_list_playlists` | "Show my playlists" |
| `aimp_play_playlist` | "Play the playlist Rammstein Mix" |
| `aimp_extend_playlist` | "Add AC/DC songs to the Rock playlist" |
| `aimp_shuffle_playlist` | "Shuffle the Rock Mix playlist and play it" |
| `aimp_delete_playlist` | "Delete playlist Old Stuff" |

### Statistics *(requires `AIMP_ADB_PATH`)*

Reads directly from AIMP's binary AudioLibrary database.

| Tool | Example prompt |
|---|---|
| `aimp_top_tracks` | "What are my 10 most played songs?" |
| `aimp_top_tracks` | "Top 20 tracks played after 2026-01-01" |
| `aimp_top_tracks` | "Tracks with at least 5 plays — play the first one" |
| `aimp_track_stats` | "How many times have I played Alison Moyet - All Cried Out?" |
| `aimp_track_stats` | "When did I last listen to Falco?" |

### Ratings *(requires `AIMP_ADB_PATH`)*

Reads the star ratings set **inside AIMP** — not the ID3 POPM tag.

| Tool | Example prompt |
|---|---|
| `aimp_search_by_rating` | "Show all 5-star songs" |
| `aimp_search_by_rating` | "Find tracks rated 3 stars or more" |
| `aimp_search_by_rating` | "Play a 4-star song" |
| `aimp_get_track_rating` | "How many stars does ZAZA - Zauberstab have?" |

---

## Troubleshooting

**Statistics/rating tools return an error**  
→ Check `AIMP_ADB_PATH` in your config. It should point to `Local.adb` in AIMP's AppData folder.  
→ Typical path: `C:\Users\YOUR_NAME\AppData\Roaming\AIMP\AudioLibrary\Local.adb`

**Year/genre search doesn't work**  
→ Run `pip install mutagen`, then restart your MCP client.

**AIMP doesn't start automatically**  
→ Check the `AIMP_EXE` path. Make sure to use double backslashes `\\` in JSON.

**No songs found**  
→ Check `AIMP_MUSIC_DIRS`. Multiple folders must be separated by `;`.

**Path errors on Windows**  
→ Always use double backslashes in JSON: `C:\\Music` not `C:\Music`.

---

## Related

- [AIMP official website](https://www.aimp.ru/)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Claude Desktop](https://claude.ai/download)
- [LM Studio](https://lmstudio.ai/)
