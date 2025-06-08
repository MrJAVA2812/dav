from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import uuid
import subprocess
import json
import re
from threading import Lock

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

download_progress = {}
progress_lock = Lock()

def get_file_size_in_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)

def sanitize_filename(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9_\-\.]+", "_", name)
    return name[:100].rstrip("_.")

@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    url = data.get("url")
    content_type = data.get("type", "video")

    if not url:
        return jsonify({"error": "Aucun lien fourni"}), 400

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "url" or info.get("is_live") or not info.get("formats"):
            return jsonify({
                "error": "Vidéo non disponible",
                "thumbnail": info.get("thumbnail"),
            }), 400

        formats = info["formats"]
        filtered_hd = []
        best_format = None
        max_height = 0
        seen = set()

        if content_type == "video":
            for fmt in formats:
                height = fmt.get("height")
                ext = fmt.get("ext")
                vcodec = fmt.get("vcodec")

                if not height or not ext or not vcodec or vcodec == "none":
                    continue

                key = (height, ext)
                if key in seen:
                    continue
                seen.add(key)

                item = {
                    "format_id": fmt["format_id"],
                    "ext": ext,
                    "resolution": f"{height}p",
                    "vcodec": vcodec,
                    "height": height
                }

                if height >= 720:
                    filtered_hd.append(item)
                elif height > max_height:
                    best_format = item
                    max_height = height

            if filtered_hd:
                return jsonify({
                    "title": info.get("title"),
                    "thumbnail": info.get("thumbnail"),
                    "formats": filtered_hd
                })
            elif best_format:
                return jsonify({
                    "message": "Aucun format HD (720p ou plus) disponible. Meilleur format trouvé :",
                    "title": info.get("title"),
                    "thumbnail": info.get("thumbnail"),
                    "formats": [best_format]
                })
            else:
                return jsonify({
                    "error": "Aucun format vidéo exploitable trouvé.",
                    "thumbnail": info.get("thumbnail")
                }), 400

        elif content_type == "audio":
            best_audio = None
            best_bitrate = 0

            for fmt in formats:
                ext = fmt.get("ext")
                abr = fmt.get("abr")
                vcodec = fmt.get("vcodec")

                if ext in ["mp3", "m4a", "webm"] and vcodec == "none":
                    if abr and abr > best_bitrate:
                        best_bitrate = abr
                        best_audio = {
                            "format_id": fmt["format_id"],
                            "ext": "MP3",
                            "abr": abr,
                            "vcodec": vcodec
                        }

            if not best_audio:
                return jsonify({
                    "error": "Aucun format audio disponible.",
                    "thumbnail": info.get("thumbnail")
                }), 400

            return jsonify({
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "formats": [best_audio]
            })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/combine", methods=["POST"])
def combine():
    data = request.get_json()
    url = data.get("url")
    format_id = data.get("format_id")
    content_type = data.get("type", "video")
    compress_to = int(data.get("compress_to", 1080))

    if not url or not format_id:
        return jsonify({"error": "Paramètres manquants"}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"Impossible d'extraire info vidéo: {str(e)}"}), 500

    title = info.get("title") or "video"
    safe_title = sanitize_filename(title)
    original_ext = "mp4" if content_type == "video" else "mp3"
    unique_id = str(uuid.uuid4())
    original_filename = os.path.join(DOWNLOAD_FOLDER, f"{unique_id}_original.{original_ext}")
    final_filename = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}.{original_ext}")

    def progress_hook(d):
        with progress_lock:
            if d.get('status') == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
                downloaded = d.get('downloaded_bytes', 0)
                percent = int(downloaded / total * 100)
                download_progress[unique_id] = percent
            elif d.get('status') == 'finished':
                download_progress[unique_id] = 100

    ydl_opts = {
        "quiet": True,
        "outtmpl": original_filename,
        "format": f"{format_id}+bestaudio/best" if content_type == "video" else format_id,
        "merge_output_format": original_ext,
        "nocheckcertificate": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if content_type == "video":
            probe_cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=height",
                "-of", "json",
                original_filename
            ]
            probe_result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            height_data = json.loads(probe_result.stdout)
            height = height_data["streams"][0]["height"]
            file_size_mb = get_file_size_in_mb(original_filename)

            if height > compress_to and file_size_mb >= 100:
                compress_cmd = [
                    "ffmpeg", "-i", original_filename,
                    "-vf", f"scale=-2:'min({compress_to},ih)'",
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    final_filename
                ]
                subprocess.run(compress_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.remove(original_filename)
            else:
                if os.path.exists(final_filename):
                    os.remove(final_filename)
                os.rename(original_filename, final_filename)
        else:
            if os.path.exists(final_filename):
                os.remove(final_filename)
            os.rename(original_filename, final_filename)

        return jsonify({
            "url": f"/file/{os.path.basename(final_filename)}",
            "id": unique_id
        })

    except Exception as e:
        return jsonify({"error": f"Téléchargement échoué : {str(e)}"}), 500


@app.route("/progress/<string:uid>")
def get_progress(uid):
    with progress_lock:
        percent = download_progress.get(uid)
    if percent is None:
        return jsonify({"progress": 0})
    return jsonify({"progress": percent})


@app.route("/file/<path:filename>")
def serve_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        return jsonify({"error": "Fichier introuvable"}), 404


if __name__ == "__main__":
    app.run(debug=True)
