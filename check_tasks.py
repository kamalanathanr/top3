"""
Diagnostic script to check task queue status
"""
import asyncio
import motor.motor_asyncio

async def main():
    client = motor.motor_asyncio.AsyncIOMotorClient('mongodb://admin:admin@localhost:27017/')
    db = client['top3']

    # Check pending queries
    pending = await db.queries.find({'status': 'pending'}).to_list(None)
    print(f'\n📋 Pending Queries: {len(pending)}')
    for q in pending[:5]:
        print(f'  • {q.get("template_name")}: {q.get("user_interests")} ({q.get("status")})')

    # Check running queries
    running = await db.queries.find({'status': 'running'}).to_list(None)
    print(f'\n▶️  Running Queries: {len(running)}')
    for q in running[:5]:
        print(f'  • {q.get("template_name")}: {q.get("user_interests")}')

    # Check completed queries
    done = await db.queries.find({'status': 'done'}).to_list(None)
    print(f'\n✅ Completed Queries: {len(done)}')

    # Check results
    results = await db.query_results.find({}).to_list(None)
    print(f'\n📊 Query Results Stored: {len(results)}')

    # Show recent queries
    recent = await db.queries.find({}).sort('created_at', -1).limit(5).to_list(None)
    print(f'\n📅 Most Recent Queries:')
    for q in recent:
        status = q.get('status', 'unknown')
        template = q.get('template_name', 'N/A')
        interests = q.get('user_interests', [])
        print(f'  • [{status}] {template}: {interests}')

    client.close()

if __name__ == '__main__':
    asyncio.run(main())
