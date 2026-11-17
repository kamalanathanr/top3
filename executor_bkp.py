import subprocess
import asyncio
from celery import Celery
import motor.motor_asyncio
from datetime import datetime
import json
import redis
from bson.objectid import ObjectId
import requests
from urllib.parse import quote

# Celery configuration
app = Celery(
    'executor',
    broker='redis://localhost:6379/0',
    backend='redis://localhost:6379/1'
)

app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes hard limit
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
)

# MongoDB setup
MONGO_URL = "mongodb://admin:admin@localhost:27017/"
DB_NAME = "top3"

async def get_db():
    """Get MongoDB connection"""
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
    return client[DB_NAME]

def search_web(query: str, num_results: int = 5) -> str:
    """Search the web for current information"""
    try:
        # Using DuckDuckGo API (no auth required)
        url = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_redirect=1"
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()
            results = []

            # Get abstract if available
            if data.get('AbstractText'):
                results.append(f"Summary: {data['AbstractText']}")

            # Get related topics
            related = data.get('RelatedTopics', [])
            for item in related[:num_results]:
                if isinstance(item, dict) and 'Text' in item:
                    results.append(item['Text'])

            return "\n".join(results) if results else "No web results found"
        else:
            return "Web search temporarily unavailable"
    except Exception as e:
        return f"Web search error: {str(e)}"

def run_claude_query(prompt: str, search_context: str = "") -> dict:
    """Execute Claude query via CLI subprocess"""
    try:
        # Enhance prompt with explicit authorization for web search
        enhanced_prompt = prompt
        if search_context:
            enhanced_prompt = f"{prompt}\n\n**Web Search Results (Already Authorized):**\n{search_context}\n\nProvide your response based on the above search results without asking for permission."
        else:
            # Add note that web search is available and don't ask for permission
            enhanced_prompt = f"{prompt}\n\nWeb search has been pre-authorized. Do not ask for permission—proceed with your response using any relevant information you can access."

        # Call claude CLI with prompt
        result = subprocess.run(
            ['claude', '-p', enhanced_prompt],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            return {
                'error': f"Claude CLI error: {result.stderr}",
                'success': False
            }

        output = result.stdout.strip()

        # Aggressively remove web search permission request messages and authorization blocks
        # Look for permission requests or authorization blocks and remove them completely
        lines = output.split('\n')
        filtered_lines = []
        skip_until_blank = False

        for line in lines:
            line_lower = line.lower()

            # Check if this line starts a permission/authorization block
            if any(phrase in line_lower for phrase in [
                'i need permission',
                'could you',
                'could you grant',
                'permission to search',
                'permission to perform',
                'authorization',
                'approved',
                'approved'
            ]) and ('permission' in line_lower or 'web search' in line_lower):
                skip_until_blank = True
                continue

            # Skip lines until we hit a blank line (end of permission block)
            if skip_until_blank:
                if line.strip() == '':
                    skip_until_blank = False
                continue

            # Keep this line
            filtered_lines.append(line)

        output = '\n'.join(filtered_lines).strip()

        # If output is empty after filtering, return error
        if not output:
            return {
                'error': 'Permission request was stripped, no content available',
                'success': False
            }

        return {
            'output': output,
            'success': True
        }
    except subprocess.TimeoutExpired:
        return {
            'error': "Claude query timed out after 5 minutes",
            'success': False
        }
    except FileNotFoundError:
        return {
            'error': "Claude CLI not found. Make sure 'claude' command is available in PATH",
            'success': False
        }
    except Exception as e:
        return {
            'error': f"Error running Claude: {str(e)}",
            'success': False
        }

@app.task(bind=True)
def execute_query(self, query_id: str):
    """
    Main task: Execute a query (AI or YouTube)
    1. Fetch query from MongoDB
    2. Check status (if paused/cancelled, skip)
    3. Route to appropriate executor (Claude AI or YouTube search)
    4. Store result in MongoDB
    5. Update query status
    """
    # Run async operation
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(_execute_query_async(query_id))
    loop.close()
    return result

async def _execute_query_async(query_id: str):
    """Async execution of query (AI or YouTube)"""
    db = await get_db()

    try:
        # Fetch query document
        query_doc = await db.queries.find_one({"_id": ObjectId(query_id)})
        if not query_doc:
            return {"error": f"Query {query_id} not found"}

        # Check if paused or cancelled
        if query_doc.get("status") in ["paused", "cancelled"]:
            return {"status": query_doc.get("status"), "skipped": True}

        # Update status to running
        await db.queries.update_one(
            {"_id": ObjectId(query_id)},
            {"$set": {"status": "running", "started_at": datetime.utcnow().isoformat()}}
        )

        # Route based on query type
        query_type = query_doc.get("query_type", "ai")
        start_time = datetime.utcnow()

        if query_type == "youtube":
            # Execute YouTube search
            interest_text = query_doc.get("user_interests", [""])[0]
            from yt_dlp import YoutubeDL

            try:
                ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist'}
                with YoutubeDL(ydl_opts) as ydl:
                    search_query = f"ytsearch5:{interest_text}"  # Get 5 videos instead of 3
                    results = ydl.extract_info(search_query, download=False)
                    videos = []
                    for video in results.get('entries', []):
                        video_id = video.get('id')
                        videos.append({
                            'id': video_id,
                            'title': video.get('title'),
                            'url': f"https://www.youtube.com/watch?v={video_id}",
                            'duration': video.get('duration', 0)
                        })
                output = {"videos": videos}
                success = True
                error_msg = None
            except Exception as e:
                output = {}
                success = False
                error_msg = str(e)
        else:
            # Execute Claude AI query
            prompt = query_doc.get("ai_query")
            template_name = query_doc.get("template_name", "")
            if not prompt:
                return {"error": "No AI query template found"}

            # Perform web search for queries that need current information
            search_context = ""
            if any(keyword in template_name.lower() for keyword in ["ironman", "event", "upcoming", "sport"]):
                # Extract search terms from interests for web search
                interests = query_doc.get("user_interests", [])
                if interests:
                    search_query = f"upcoming {interests[0]} events 2026"
                    search_context = search_web(search_query)
                    # Prepend instruction to use the search context
                    prompt = f"{prompt}\n\n** IMPORTANT INSTRUCTION **\nWeb search permission has been PRE-APPROVED by the user. You DO NOT need to ask for permission. Use the following real-time event information in your response:\n\n{search_context}\n\nDo NOT output any permission request or authorization message. Simply provide your response using the above information."

            claude_result = run_claude_query(prompt, "")  # Pass empty string since context is in prompt

            success = claude_result.get('success')
            error_msg = claude_result.get('error') if not success else None

            # Parse Claude output - assume JSON format
            if success:
                try:
                    output = json.loads(claude_result.get('output', '{}'))
                except json.JSONDecodeError:
                    output = {"text": claude_result.get('output')}

        # Calculate time taken
        time_taken = (datetime.utcnow() - start_time).total_seconds()

        if not success:
            # Store error result
            error_result = {
                "query_id": ObjectId(query_id),
                "user_id": query_doc.get("user_id"),
                "output": {"error": error_msg},
                "time_taken_sec": time_taken,
                "completed_at": datetime.utcnow().isoformat()
            }
            await db.query_results.insert_one(error_result)

            # Update query status to done with error
            await db.queries.update_one(
                {"_id": ObjectId(query_id)},
                {
                    "$set": {
                        "status": "done",
                        "completed_at": datetime.utcnow().isoformat(),
                        "error": error_msg
                    }
                }
            )
            return {"error": error_msg}

        # Create result document
        result_doc = {
            "query_id": ObjectId(query_id),
            "user_id": query_doc.get("user_id"),
            "template_name": query_doc.get("template_name"),
            "output": output,
            "time_taken_sec": time_taken,
            "completed_at": datetime.utcnow().isoformat()
        }

        # Insert result
        insert_result = await db.query_results.insert_one(result_doc)

        # Update query status to done
        await db.queries.update_one(
            {"_id": ObjectId(query_id)},
            {
                "$set": {
                    "status": "done",
                    "completed_at": datetime.utcnow().isoformat(),
                    "result_id": str(insert_result.inserted_id)
                }
            }
        )

        return {
            "query_id": query_id,
            "status": "done",
            "time_taken": time_taken,
            "result_id": str(insert_result.inserted_id)
        }

    except Exception as e:
        print(f"Error executing query {query_id}: {str(e)}")
        # Update query status to error
        try:
            await db.queries.update_one(
                {"_id": ObjectId(query_id)},
                {
                    "$set": {
                        "status": "error",
                        "error": str(e),
                        "completed_at": datetime.utcnow().isoformat()
                    }
                }
            )
        except:
            pass
        return {"error": str(e)}

if __name__ == '__main__':
    app.start()
