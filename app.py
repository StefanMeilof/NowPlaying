import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from anthropic import Anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

# Clients
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Tool definitions for Claude
tools = [
    {
        "name": "add_video",
        "description": "Add a YouTube video to the user's NowPlaying page. Call this when the user pastes a YouTube URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "youtube_url": {
                    "type": "string",
                    "description": "The full YouTube URL"
                }
            },
            "required": ["youtube_url"]
        }
    },
    {
        "name": "remove_video",
        "description": "Remove a video from the user's NowPlaying page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "video_id": {
                    "type": "string",
                    "description": "The UUID of the video to remove"
                }
            },
            "required": ["video_id"]
        }
    },
    {
        "name": "list_videos",
        "description": "List all videos currently on the user's NowPlaying page.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
]


# --- YouTube oEmbed ---

def fetch_oembed(youtube_url):
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": youtube_url, "format": "json"},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "title": data.get("title", "Unknown title"),
                "thumbnail_url": data.get("thumbnail_url", ""),
                "author_name": data.get("author_name", ""),
            }
    except Exception as e:
        print(f"oEmbed error: {e}")
    return None


# --- Tool execution ---

def execute_tool(tool_name, tool_input, user_id):
    if tool_name == "add_video":
        youtube_url = tool_input["youtube_url"]
        meta = fetch_oembed(youtube_url)
        if not meta:
            return "Could not fetch video details. Please check the URL and try again."
        result = supabase.table("videos").insert({
            "user_id": user_id,
            "youtube_url": youtube_url,
            "title": meta["title"],
            "thumbnail_url": meta["thumbnail_url"],
            "category": meta["author_name"],
        }).execute()
        return f"Added: {meta['title']} is now on your page for 10 days."

    elif tool_name == "remove_video":
        video_id = tool_input["video_id"]
        supabase.table("videos").delete().eq("id", video_id).eq("user_id", user_id).execute()
        return "Done. Video removed from your page."

    elif tool_name == "list_videos":
        result = supabase.table("videos").select("*").eq("user_id", user_id).execute()
        videos = result.data
        if not videos:
            return "Your page is empty. Paste a YouTube link to add something."
        lines = []
        for v in videos:
            expires = datetime.fromisoformat(v["expires_at"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_left = (expires - now).days + 1
            lines.append(f"· {v['title']} ({days_left}d left) — id: {v['id']}")
        return "\n".join(lines)

    return "Unknown tool."


# --- Chat endpoint ---

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    user_id = data.get("user_id")
    messages = data.get("messages", [])

    if not user_id or not messages:
        return jsonify({"error": "Missing user_id or messages"}), 400

    system_prompt = (
        "You are the NowPlaying assistant. You help users manage their public music page. "
        "Users can add YouTube videos, remove videos, and check what is currently on their page. "
        "When a user pastes a YouTube URL, always call add_video. "
        "Keep responses short and friendly. "
        "When listing videos, show the title, days remaining, and mention the id only if the user wants to remove one."
    )

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system_prompt,
        tools=tools,
        messages=messages
    )

    # Handle tool use
    if response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input, user_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        # Send tool results back to Claude for a natural reply
        messages_with_results = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results}
        ]
        final_response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system_prompt,
            tools=tools,
            messages=messages_with_results
        )
        reply = next((b.text for b in final_response.content if hasattr(b, "text")), "Done.")
    else:
        reply = next((b.text for b in response.content if hasattr(b, "text")), "Done.")

    return jsonify({"reply": reply})


# --- Public profile endpoint ---

@app.route("/user/<username>", methods=["GET"])
def get_profile(username):
    profile = supabase.table("profiles").select("*").eq("username", username).single().execute()
    if not profile.data:
        return jsonify({"error": "User not found"}), 404

    videos = supabase.table("videos").select("*").eq("user_id", profile.data["id"]).execute()

    return jsonify({
        "profile": profile.data,
        "videos": videos.data
    })


# --- Health check ---

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "NowPlaying API"})


# --- Nightly expiry job ---

def delete_expired_videos():
    now = datetime.now(timezone.utc).isoformat()
    result = supabase.table("videos").delete().lt("expires_at", now).execute()
    print(f"[cron] Deleted expired videos at {now}")

scheduler = BackgroundScheduler()
scheduler.add_job(delete_expired_videos, "cron", hour=3, minute=0)
scheduler.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
