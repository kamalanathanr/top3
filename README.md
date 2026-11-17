# YouTube Player Website

A simple, clean website built with **FastAPI**, **HTMX**, and **Tailwind CSS** that displays and auto-plays a YouTube video after 5 seconds.

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

docker compose up -d
docker compose ps


### 2. Run the Server
```bash
python app.py
```

The server will start at `http://localhost:8000`

### 3. Open in Browser
Visit http://localhost:8000 and the video will automatically start playing after 5 seconds.

## Stack

- **FastAPI**: Modern, fast Python web framework
- **Tailwind CSS**: Utility-first CSS for quick styling (loaded via CDN)
- **HTMX**: Makes HTML interactive (included for future enhancements)
- **YouTube Embed**: Native iframe with autoplay parameter

## File Structure

```
.
├── app.py              # FastAPI application
├── requirements.txt    # Python dependencies
└── templates/
    └── index.html      # HTML template with embedded video
```

## How It Works

1. FastAPI serves the HTML template
2. The page loads with the video in a paused state
3. After 5 seconds, JavaScript changes the iframe src to enable autoplay
4. Video starts playing automatically

## Customization

To change the video, replace the video ID `Qr_Lr4Dt-oo` in both:
- `templates/index.html` (in the iframe src and footer)
- The YouTube link

Example: For URL `https://www.youtube.com/watch?v=VIDEO_ID`, use `VIDEO_ID` in the embed.




Step 1: Start MongoDB & Redis with Docker

cd /Users/kamalnathan/Documents/projects/top3
docker compose up -d
This will:

Download MongoDB 7 and Redis 7 images (first time only)
Start both services in the background
Create persistent volumes for data
Step 2: Verify they're running

docker compose ps
You should see:


NAME              STATUS
top3-mongo        Up (healthy)
top3-redis        Up (healthy)
Step 3: Now start the FastAPI app

python app.py
You should see:


INFO:     Uvicorn running on http://0.0.0.0:8000
MongoDB connected and initialized
Step 4: In a new terminal, start the Celery worker

cd /Users/kamalnathan/Documents/projects/top3

source .venv/bin/activate

celery -A executor worker --loglevel=info
You should see:


celery@... ready to accept tasks
Troubleshooting
If Docker is not installed:


# Install Docker Desktop from: https://www.docker.com/products/docker-desktop
# Then retry: docker compose up -d
If you get "docker: command not found":

You need Docker Desktop installed on your Mac
Download from: https://www.docker.com/products/docker-desktop
After install, restart your terminal
If MongoDB container won't start:


docker compose down -v  # Remove everything
docker compose up -d    # Start fresh
Try the Docker steps and let me know what happens!


Run this to see which interests have results:
python3 check_interests.py


celery -A src.tasks.celery_app worker --loglevel=info
python app.py


celery -A src.tasks.celery_app worker --pool=threads --concurrency=4 --loglevel=info


=========

# Stop every worker connected to this broker
celery -A src.tasks.celery_app control shutdown

or just kill it
ps aux | grep "celery" | grep -v grep

# Wait a moment, then start clean
celery -A src.tasks.celery_app worker --pool=threads --concurrency=4 --loglevel=info




Start docker

cd /Users/kamalnathan/Documents/projects/top3
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
celery -A src.tasks.celery_app worker --loglevel=info


source .venv/bin/activate
python3 app.py

source .venv/bin/activate
pip3 install playwright && playwright install chromium 