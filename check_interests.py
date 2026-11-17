"""
Check which interests have results
"""
import asyncio
import motor.motor_asyncio
from collections import defaultdict

async def main():
    client = motor.motor_asyncio.AsyncIOMotorClient('mongodb://admin:admin@localhost:27017/')
    db = client['top3']

    # Get all queries grouped by interest
    all_queries = await db.queries.find({}).to_list(None)
    queries_by_interest = defaultdict(list)

    for q in all_queries:
        interests = q.get('user_interests', [])
        for interest in interests:
            queries_by_interest[interest].append({
                'template': q.get('template_name'),
                'status': q.get('status'),
                'type': q.get('query_type')
            })

    print(f'\n📌 Interests Added:')
    for interest, queries in sorted(queries_by_interest.items()):
        print(f'\n  Interest: "{interest}"')
        for q in queries:
            status_icon = '✅' if q['status'] == 'done' else '⏳' if q['status'] == 'running' else '⏸'
            print(f'    {status_icon} {q["template"]}: {q["status"]}')

    # Get all results grouped by interest
    all_results = await db.query_results.find({}).to_list(None)
    results_by_interest = defaultdict(int)

    for result in all_results:
        query = result.get('query', [])
        if query:
            interests = query[0].get('user_interests', [])
            for interest in interests:
                results_by_interest[interest] += 1

    print(f'\n\n📊 Results by Interest:')
    for interest, count in sorted(results_by_interest.items()):
        print(f'  • "{interest}": {count} results')

    # Check the latest interest added
    interests = await db.interests.find({}).sort('added_at', -1).limit(1).to_list(None)
    if interests:
        latest = interests[0]
        print(f'\n\n🆕 Latest Interest Added:')
        print(f'  Text: "{latest.get("text")}"')
        print(f'  Added at: {latest.get("added_at")}')

        # Check for queries for this interest
        interest_queries = await db.queries.find({'user_interests': latest.get("text")}).to_list(None)
        print(f'  Queries created: {len(interest_queries)}')
        for q in interest_queries:
            print(f'    • {q.get("template_name")}: {q.get("status")}')

    client.close()

if __name__ == '__main__':
    asyncio.run(main())
