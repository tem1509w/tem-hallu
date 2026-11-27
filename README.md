# Discord Music Bot — đa nền tảng, Last.fm, 24/7

Bot nhạc Discord viết bằng Python 3.11+, dùng `discord.py`, `yt-dlp` và FFmpeg.

## Tính năng

- **Đa nền tảng**: link YouTube/YouTube Music, SoundCloud, Bandcamp, Vimeo, Twitch, Mixcloud, Audius, link audio trực tiếp và các trang yt-dlp hỗ trợ. Tìm kiếm mặc định trên YouTube.
- **Spotify**: đọc metadata bài hát/album/playlist qua Spotify Web API rồi ghép sang nguồn phát tương ứng. Spotify không cho bot stream trực tiếp qua API.
- **Last.fm scrobbling**: cập nhật now playing, tự scrobble khi nghe đủ ngưỡng, xem playcount của tài khoản và thống kê scrobble của bot.
- **Player cơ bản**: play, playlist, pause, resume, skip, stop, queue, nowplaying, loop (off/track/queue), shuffle, seek, volume, remove, clear, lịch sử.
- **24/7**: lưu kênh voice vào SQLite; tự kết nối lại có backoff khi mất kết nối; tự vào lại kênh sau khi restart. Có lệnh bật/tắt và xem trạng thái.
- Slash command và prefix command (mặc định `m!`).

## Yêu cầu

- Python **3.11+**
- **FFmpeg** trong `PATH` (hoặc đặt `FFMPEG_PATH`)
- `yt-dlp` (được cài từ `requirements.txt`)
- `libopus` cho Discord voice
- Bot token từ Discord Developer Portal
- Last.fm API account nếu muốn scrobble
- Spotify Client ID/Secret nếu muốn dùng link Spotify

### Windows

```powershell
winget install Gyan.FFmpeg
```

Cài xong mở PowerShell mới và kiểm tra:

```powershell
ffmpeg -version
```

### Debian/Ubuntu

```bash
sudo apt update
sudo apt install -y ffmpeg libopus0
```

## Cài đặt

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Mở `.env` và điền tối thiểu:

```dotenv
DISCORD_TOKEN=...
```

Chạy bot:

```powershell
python main.py
```

Trên Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## Deploy lên Railway

Repo có sẵn `Dockerfile` (Python 3.12 + FFmpeg + libopus), Railway build trực tiếp từ Dockerfile đó nên không phải cài gì thêm.

### 1. Tạo project

1. Đăng nhập <https://railway.com> bằng GitHub.
2. **New Project** → **Deploy from GitHub repo** → cài Railway GitHub App → chọn `tem1509w/tem-hallu`.
3. Railway tự nhận `Dockerfile` và build. Deploy đầu tiên sẽ dừng vì thiếu token — bình thường, làm bước 2.

Bot là worker (không mở port), không cần tạo domain.

### 2. Thêm biến môi trường

Service → **Variables** → **Raw Editor**, dán:

```dotenv
DISCORD_TOKEN=...
DATABASE_PATH=/data/musicbot.sqlite3
DEV_GUILD_ID=...
LOG_LEVEL=INFO
```

`DEV_GUILD_ID` là ID server test (bật Developer Mode → chuột phải server → Copy Server ID), giúp slash command hiện ngay thay vì chờ tới 1 giờ.

Nếu cần Last.fm/Spotify thì thêm `LASTFM_API_KEY`, `LASTFM_API_SECRET`, `LASTFM_SESSION_KEY`, `LASTFM_USERNAME`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`.

### 3. Gắn volume cho SQLite

Filesystem mặc định là tạm, nên mỗi lần deploy bot sẽ mất trạng thái 24/7 và lịch sử.

1. Project canvas → **+ Create** → **Volume** (hoặc `Ctrl+K` → "volume").
2. Gán cho service bot, mount path `/data`.
3. Redeploy để container nhận volume.

Volume được mount lúc container start và mount bằng root; image này chạy root nên không cần đổi UID.

### 4. Cấu hình service

- Settings → **Restart Policy**: `Always`.
- Giữ **1 replica**: nhiều replica sẽ tạo nhiều voice client và scrobble trùng.
- Không cần Healthcheck Path vì bot không có HTTP server.

### 5. Add bot vào server

Làm mục **Cấu hình Discord** bên dưới, nhớ bật **Message Content Intent**. Link mời nhanh, thay `<CLIENT_ID>` bằng Application ID:

```text
https://discord.com/api/oauth2/authorize?client_id=<CLIENT_ID>&permissions=3230720&scope=bot%20applications.commands
```

`3230720` = View Channels + Send Messages + Embed Links + Read Message History + Connect + Speak.

### 6. Chạy thử

Trong server test, vào một voice channel rồi:

1. `/play never gonna give you up` — bot join và phát.
2. `/queue`, `/nowplaying`, `/pause`, `/skip`, `/volume 50`.
3. `/247 on` — bot ở lại kênh. Redeploy service, bot phải tự vào lại kênh đó.
4. `/lastfm` — xem playcount và số scrobble.

Xem log ở **Deployments** → **View Logs**, hoặc `railway logs`.

### 7. Kiểm tra môi trường trong container

```powershell
npm i -g @railway/cli
railway login
railway link
railway ssh -- ffmpeg -version
railway ssh -- python -c "import discord.opus as o; o.load_opus('libopus.so.0'); print(o.is_loaded())"
```

### 8. Nếu YouTube chặn IP

IP datacenter hay bị YouTube yêu cầu xác minh. Xuất cookies dạng Netscape từ trình duyệt rồi upload lên volume:

```powershell
railway volume files upload ./cookies.txt /cookies.txt
```

Đặt `YTDLP_COOKIES_FILE=/cookies.txt` rồi redeploy. Nên dùng tài khoản phụ vì cookie là thông tin đăng nhập.

### Lưu ý về voice trên Railway

Discord voice dùng UDP ra ngoài. Railway không có công tắc bật/tắt UDP, nhưng **Static Outbound IPs** (tính năng Pro) không hỗ trợ outbound UDP — nếu đang bật thì tắt. Cũng có báo cáo bot join được kênh nhưng kẹt ở bước UDP handshake; đó là vấn đề đường mạng của nền tảng, không phải code. Nếu gặp: thử đổi region của service, hoặc chạy trên VPS theo mục 24/7 phía trên.

### Chi phí

Railway tính theo RAM/CPU thực dùng. Bot voice thường dùng 0,2–0,4 GB RAM; gói **Hobby 5 USD/tháng** (kèm 5 USD usage) phù hợp cho bot chạy liên tục, gói Free chỉ có 1 USD usage nên không đủ. Nên đặt **Spend limit** trong Settings.

## Cấu hình Discord

1. Vào [Discord Developer Portal](https://discord.com/developers/applications), tạo Application và Bot.
2. Copy **Bot Token** vào `DISCORD_TOKEN`.
3. Bật **MESSAGE CONTENT INTENT** trong mục Bot (cần cho prefix command).
4. Invite bot với scopes `bot` và `applications.commands`, quyền:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Connect
   - Speak
5. Người dùng cần quyền **Manage Server** để bật/tắt 24/7.

Slash command toàn cầu có thể mất tới khoảng 1 giờ mới hiện. Khi phát triển, đặt `DEV_GUILD_ID` để đồng bộ ngay vào server thử nghiệm.

## Last.fm

### 1. Tạo API account

Truy cập <https://www.last.fm/api/account/create>, tạo ứng dụng và lấy:

- API key
- Shared secret

Điền vào `.env`:

```dotenv
LASTFM_API_KEY=...
LASTFM_API_SECRET=...
```

### 2. Lấy session key

```powershell
python scripts/lastfm_auth.py
```

Script mở link ủy quyền Last.fm, sau khi bấm **Authorize** sẽ in ra:

```dotenv
LASTFM_SESSION_KEY=...
LASTFM_USERNAME=...
```

Thêm hai dòng đó vào `.env`. Session key là bí mật; không commit `.env`.

### Ngưỡng scrobble

Bot scrobble khi:

- bài dài ít nhất 30 giây;
- đã nghe ít nhất **1/2 thời lượng**, hoặc tối đa **4 phút**;
- không phải luồng live.

Đây là quy tắc của Last.fm. Bot cập nhật now playing ngay khi bắt đầu phát, và scrobble khi đạt ngưỡng (không cần đợi hết bài). Tài khoản Last.fm là **một tài khoản dùng chung cho toàn bot**; mọi server scrobble về cùng user.

`/lastfm` hiển thị tổng playcount Last.fm và thống kê local. `/stats` và `/history` chỉ dùng dữ liệu SQLite của bot.

## Spotify

1. Tạo app tại <https://developer.spotify.com/dashboard>.
2. Lấy **Client ID** và **Client Secret** (Client Credentials Flow, không cần redirect URI).
3. Điền:

```dotenv
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
```

Bot hỗ trợ link `open.spotify.com/track|album|playlist/...` và URI `spotify:track:...`.

> Spotify API không cấp stream audio cho bot. Bot đọc tên bài/nghệ sĩ/thumbnail rồi tìm nguồn phát tương ứng. Vì vậy kết quả ghép có thể sai với bản thu hiếm hoặc nội dung không có trên nguồn phát.

## Lệnh

| Lệnh | Chức năng |
|---|---|
| `/play <tên hoặc link>` | Phát bài hát, tự join voice của bạn |
| `/playlist <link>` | Thêm playlist/album |
| `/pause`, `/resume` | Tạm dừng / tiếp tục |
| `/skip`, `/stop` | Bỏ qua / dừng và xóa queue |
| `/seek <90 hoặc 1:30>` | Tua bài hiện tại |
| `/volume [0-200]` | Xem/đổi âm lượng |
| `/loop [off/track/queue]` | Xem/đổi chế độ lặp |
| `/queue [trang]` | Xem hàng đợi |
| `/nowplaying` | Bài đang phát |
| `/shuffle`, `/remove <vị trí>`, `/clear` | Quản lý hàng đợi |
| `/join`, `/leave` | Join / rời voice, tắt 24/7 |
| `/247 on|off|status` | Bật/tắt/xem 24/7 |
| `/lastfm` | Playcount Last.fm + thống kê |
| `/stats`, `/history` | Thống kê và lịch sử local |
| `/help` | Hướng dẫn |

Prefix tương đương: `m!play ...`, `m!247 on`, v.v. Alias tiện dụng: `m!p`, `m!np`, `m!q`, `m!pl`, `m!fm`.

## Cách 24/7 hoạt động

- `/247 on` lưu `guild_id`, `voice_channel_id` và cờ bật vào `data/musicbot.sqlite3`.
- Khi bot bị ngắt voice, bot thử kết nối lại với backoff 2s → 4s → ... → tối đa `RECONNECT_MAX_DELAY_SECONDS`.
- Khi bot restart, `on_ready` đọc SQLite và tự vào lại kênh đã lưu.
- Nếu bot bị di chuyển voice bởi admin, kênh 24/7 mới được cập nhật.
- **Hàng đợi và bài đang phát không được lưu qua restart**; sau restart bot vào kênh và chờ lệnh mới.
- Nếu mất kết nối tạm thời, bot cố phát tiếp bài hiện tại từ vị trí gần nhất.
- `/247 off` chỉ tắt tự reconnect; bot vẫn ở voice. `/leave` tắt 24/7 và rời voice.

Chạy bot dưới process supervisor để đảm bảo 24/7 thực sự:

```ini
# /etc/systemd/system/ru-music-bot.service
[Unit]
Description=Discord Music Bot
After=network-online.target

[Service]
Type=simple
User=musicbot
WorkingDirectory=/opt/ru-music-bot
ExecStart=/opt/ru-music-bot/.venv/bin/python main.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/ru-music-bot/.env

[Install]
WantedBy=multi-user.target
```

## Biến môi trường

Xem `.env.example`. Các biến quan trọng:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `DISCORD_TOKEN` | — | Bắt buộc |
| `COMMAND_PREFIX` | `m!` | Prefix command |
| `FFMPEG_PATH` | `ffmpeg` | Đường dẫn FFmpeg |
| `YTDLP_PATH` | `yt-dlp` | Đường dẫn yt-dlp |
| `YTDLP_COOKIES_FILE` | — | Cookies Netscape khi YouTube chặn IP |
| `DATABASE_PATH` | `data/musicbot.sqlite3` | SQLite |
| `DEFAULT_VOLUME` | `80` | 0–200 |
| `MAX_QUEUE_SIZE` | `500` | Số bài chờ tối đa |
| `MAX_PLAYLIST_SIZE` | `50` | Số bài tối đa mỗi playlist |
| `DEV_GUILD_ID` | — | Sync slash nhanh vào 1 server |
| `LASTFM_*` | — | Scrobble/playcount |
| `SPOTIFY_*` | — | Metadata Spotify |

## Cấu trúc

```
main.py                 # entrypoint, logging
bot.py                  # bot lifecycle, sync, error handler
config.py               # đọc/validate .env
cogs/music.py           # lệnh phát nhạc và 24/7
cogs/stats.py           # Last.fm/local stats
music/resolver.py       # yt-dlp + Spotify resolution
music/player.py         # queue, playback, scrobble watcher, reconnect
music/lastfm.py         # Last.fm API (signature, now playing, scrobble)
music/storage.py        # SQLite (24/7 + lịch sử)
scripts/lastfm_auth.py  # tạo session key
```

## Troubleshooting

- **`Không tìm thấy yt-dlp`**: kích hoạt venv rồi chạy `python -m pip install -U yt-dlp`.
- **YouTube báo bot/sign-in**: cập nhật `yt-dlp`, hoặc xuất cookies trình duyệt dạng Netscape và đặt `YTDLP_COOKIES_FILE`.
- **Bot join nhưng không có tiếng**: kiểm tra FFmpeg, quyền **Speak**, và `libopus`.
- **Slash command chưa hiện**: chờ Discord sync, hoặc dùng `DEV_GUILD_ID`, khởi động lại client Discord.
- **Last.fm không tăng count**: kiểm tra đủ 4 biến Last.fm; scrobble chỉ chạy sau ngưỡng; bài live/dưới 30 giây bị bỏ qua.
- **Link Spotify bị từ chối**: cần Spotify Client ID/Secret. Link album/playlist dùng `/playlist`, không dùng `/play`.
- **24/7 không vào lại**: kiểm tra bot còn ở server, kênh voice còn tồn tại, bot có quyền Connect/Speak; xem log reconnect.
- **Lỗi database**: thư mục chứa `DATABASE_PATH` phải ghi được.

## Lưu ý

- Chỉ phát nội dung bạn có quyền sử dụng. Tuân thủ điều khoản của Discord, YouTube, SoundCloud, Spotify và các nền tảng khác.
- Không commit `.env`, cookies hoặc database chứa dữ liệu server.
- Last.fm session key và token Discord là thông tin bí mật.
