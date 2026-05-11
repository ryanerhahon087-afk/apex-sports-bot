import asyncio, sys, json
sys.path.insert(0, '.')

async def probe():
    from data.kalshi_client import SportsKalshiClient
    from config.config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY, KALSHI_BASE_URL

    client = SportsKalshiClient(KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY,
                                 KALSHI_BASE_URL, paper_mode=True)
    await client.connect()

    # Step 1: Get ALL available series
    print('=== ALL AVAILABLE SERIES ===')
    data = await client._get('/series', {'limit': 100})
    series_list = data.get('series', [])
    for s in series_list:
        ticker = s.get('ticker', '')
        title = s.get('title', '')
        if any(x in ticker.upper() for x in ['NBA', 'MLB', 'NFL', 'NHL', 'SOCCER', 'MLS', 'SPORT', 'GAME', 'TEAM']):
            print(f'  {ticker:30} | {title}')

    print()
    print('=== SAMPLE MARKETS PER SPORTS SERIES ===')

    # Step 2: For each sports series get sample markets
    sports_series = [s.get('ticker') for s in series_list
                     if any(x in s.get('ticker', '').upper()
                     for x in ['NBA', 'MLB', 'NFL', 'NHL', 'SOCCER', 'MLS'])]

    for series in sports_series[:10]:
        data = await client._get('/markets', {
            'series_ticker': series,
            'status': 'open',
            'limit': 3
        })
        markets = data.get('markets', [])
        if markets:
            m = markets[0]
            print(f'{series}: {len(markets)} markets')
            print(f'  Sample: {m.get("title", "")[:60]}')
            print(f'  Ticker: {m.get("ticker", "")}')
            print(f'  yes_ask={float(m.get("yes_ask_dollars") or 0):.2f} yes_bid={float(m.get("yes_bid_dollars") or 0):.2f}')
            print(f'  close_time={m.get("close_time", "")[:16]}')
            print(f'  event_ticker={m.get("event_ticker", "")}')
            print()
        else:
            print(f'{series}: 0 open markets')

    print()
    print('=== FULL MARKET STRUCTURE (first NBA market) ===')
    data = await client._get('/markets', {
        'series_ticker': 'KXNBAGAME',
        'status': 'open',
        'limit': 1
    })
    markets = data.get('markets', [])
    if markets:
        print(json.dumps(markets[0], indent=2, default=str))
    else:
        print('No open KXNBAGAME markets right now')
        # Fallback: try KXMLBGAME
        data = await client._get('/markets', {
            'series_ticker': 'KXMLBGAME',
            'status': 'open',
            'limit': 1
        })
        markets = data.get('markets', [])
        if markets:
            print('=== FULL MARKET STRUCTURE (first MLB market instead) ===')
            print(json.dumps(markets[0], indent=2, default=str))
        else:
            print('No open KXMLBGAME markets either')

    await client.disconnect()

asyncio.run(probe())
