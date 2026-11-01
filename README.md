# AJoeChat

AJoeChat is a temporary chat room with a Python server and a plain HTML, CSS, and JavaScript interface. One service serves the page, API, uploads, and live chat connection on port 6772. People join with a username and do not need an account.

## Run the app

Use Python 3.11 or newer. Install the packages in `requirements.txt`, then start the server:

```text
pip install -r requirements.txt
python3 main.py
```

Open `http://127.0.0.1:6772` in your browser.

The server listens on `0.0.0.0` and port `6772` by default. It uses the `PORT` environment value when one is provided. Set `AJOECHAT_HOST`, `AJOECHAT_PORT`, or `AJOECHAT_DATA` to change the host, local port, or data directory.

## Check the app

Run the automated checks with:

```text
python3 -m unittest discover -s tests -v
```

## Chat behavior

1. Usernames contain 1 to 24 letters, numbers, or underscores. AJoeChat normalizes Unicode and treats names without regard to case.
2. A username stays reserved while its session is active. If a browser closes without leaving, the reservation ends after 75 seconds without a heartbeat.
3. Text messages can contain up to 3000 Unicode code points.
4. People can share JPEG, PNG, and WebP images, MP4 video, MP3 audio, TXT files, PDFs, and DOCX documents. Each upload can be up to 5 MB. The server checks the file content before storing it.
5. Images appear in the chat and can be opened in a larger view. Other files appear with a clear type label and can be downloaded.
6. Messages and uploads expire within 24 hours. A background task removes expired messages and files.
7. The room opens with the latest 100 messages and loads older messages as you scroll up.
8. The server shares new messages and room presence over a WebSocket connection.
9. Select a person in the people list to open a private one to one chat. Private messages and files are shared only with the two people in that chat.
10. New messages appear in one closable notification card. A new message replaces the notification already shown.
11. Private chat history belongs to each active session. Reusing a username does not grant access to older private messages.

## Deployment

AJoeChat runs as one Python process on one host. The same service serves the frontend, API, uploads, and live chat connection on port 6772. The server uses SQLite and a local directory for temporary data. Set `AJOECHAT_COOKIE_SECURE=1` when the public site uses HTTPS.

### Render

Create one Python web service. Use `pip install -r requirements.txt` as the build command and `python3 main.py` as the start command. Set the health check path to `/health` and set the service value `PORT` to `6772` in the Render settings. The backend serves the frontend and API on that same port.

Render uses a temporary local filesystem by default. On a Free web service, Render can stop the service after 15 minutes without inbound traffic. When that happens, the local SQLite database and uploaded files are lost. A restart or deploy can also clear them. When AJoeChat starts again, it creates a fresh state and people join again. Messages and files can therefore disappear before their 24 hour expiry. This is expected for a temporary chat room. See the [Render Free service details](https://render.com/docs/free).

### Railway

The `railway.json` file sets the Python start command and health check. Set `PORT` to `6772` in the Railway service settings. Railway and Render both serve the frontend, API, uploads, and live chat connection through the Python process.

AJoeChat keeps WebSocket presence and file access on the local application instance. Run one process for this release. Multiple app instances need shared session coordination, message delivery, and file storage before they can serve one consistent room.

The server does not store passwords or verified identities. A username reserves a name for an active session but does not prove who a person is.

## Interface

The interface is designed for desktop and mobile screens. It supports keyboard use, visible focus, reduced motion preferences, inline image previews, and a larger image view. Notifications stay visible until closed, and a new message replaces the one already shown. The interface uses local system fonts and does not need an external asset service.
