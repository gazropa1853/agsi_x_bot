"""Daily Polish public net generation mix. Default: preview, --publish: Buffer queue."""
import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

WARSAW = ZoneInfo('Europe/Warsaw')
GROUPS = {
    'Solar': 'PV',
    'Fossil hard coal': 'Węgiel',
    'Fossil brown coal / lignite': 'Węgiel',
    'Fossil gas': 'Gaz',
    'Wind onshore': 'Wiatr',
    'Wind offshore': 'Wiatr',
    'Biomass': 'Biomasa',
    'Hydro Run-of-River': 'Woda',
    'Hydro water reservoir': 'Woda',
    'Hydro pumped storage': 'Woda',
    'Battery': 'Pozostałe',
    'Fossil oil': 'Pozostałe',
    'Fossil coal-derived gas': 'Pozostałe',
    'Others': 'Pozostałe',
    'Other renewables': 'Pozostałe',
}
EXCLUDED = {
    'Hydro pumped storage consumption', 'Battery Consumption',
    'Cross border electricity trading', 'Load', 'Residual load',
    'Renewable share of load', 'Renewable share of generation',
}
REQUIRED = {'Solar', 'Fossil hard coal', 'Fossil brown coal / lignite',
            'Fossil gas', 'Wind onshore'}


def bounds(day):
    start = datetime.combine(day, time.min, WARSAW)
    end = datetime.combine(day + timedelta(days=1), time.min, WARSAW)
    return start, end


def fetch(day):
    start, end = bounds(day)
    response = requests.get('https://api.energy-charts.info/public_power', params={
        'country': 'pl', 'start': start.isoformat(), 'end': end.isoformat(),
    }, timeout=90)
    response.raise_for_status()
    return response.json()


def aggregate(payload, day):
    if payload.get('deprecated'):
        raise ValueError('Endpoint oznaczony jako deprecated. Sprawdź dokumentację.')
    start, end = bounds(day)
    lo, hi = int(start.timestamp()), int(end.timestamp())
    stamps = payload['unix_seconds']
    if stamps != sorted(set(stamps)):
        raise ValueError('Powtórzone lub nieuporządkowane znaczniki czasu.')
    indices = [i for i, t in enumerate(stamps) if lo <= t < hi]
    # This endpoint currently returns quarter-hourly MW; fail closed on resolution change.
    expected = list(range(lo, hi, 900))
    if [stamps[i] for i in indices] != expected:
        raise ValueError('Niekompletna doba lub zmieniona rozdzielczość API. Post pominięty.')
    rows = payload['production_types']
    names = [row['name'] for row in rows]
    if len(names) != len(set(names)):
        raise ValueError('Powtórzona kategoria w API.')
    unknown = set(names) - GROUPS.keys() - EXCLUDED
    if unknown:
        raise ValueError(f'Nowe kategorie wymagają sprawdzenia: {sorted(unknown)}')
    if REQUIRED - set(names):
        raise ValueError(f'Brak podstawowych źródeł: {sorted(REQUIRED - set(names))}')
    energy = defaultdict(float)
    for row in rows:
        if row['name'] in EXCLUDED:
            continue
        values = row['data']
        if len(values) != len(stamps):
            raise ValueError(f'Nieprawidłowa długość serii: {row["name"]}')
        for i in indices:
            value = values[i]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f'Brak lub błędna wartość: {row["name"]}, {stamps[i]}')
            energy[GROUPS[row['name']]] += value * 0.25
    if sum(energy.values()) <= 0:
        raise ValueError('Brak produkcji.')
    return dict(energy)


def make_post(energy, day):
    total = sum(energy.values())
    # Largest remainder method: integer percentages sum to exactly 100.
    exact = {k: 100 * v / total for k, v in energy.items() if v > 0}
    rounded = {k: math.floor(v) for k, v in exact.items()}
    order = sorted(exact, key=lambda k: (-(exact[k] - rounded[k]), k))
    for k in order[:100 - sum(rounded.values())]:
        rounded[k] += 1
    lines = [f'Miks produkcji prądu w Polsce ({day:%d.%m.%Y})', '']
    for name in sorted(exact, key=lambda k: (-energy[k], k)):
        lines.append(f'{name}: {rounded[name]}%')
    lines += ['', 'Źródło: Energy-Charts / Fraunhofer ISE', 'Obliczenia własne.']
    text = '\n'.join(lines)
    if len(text) > 280:
        raise ValueError('Post przekracza 280 znaków.')
    return text


def publish(text):
    # Same mutation and scheduling mode as the working AGSI pipeline.
    # Inline input avoids assuming the GraphQL input type name.
    query = '''mutation { createPost(input: {
      text: %s, channelId: %s, schedulingType: automatic, mode: addToQueue
    }) { ... on PostActionSuccess { post { id } }
         ... on MutationError { message } } }''' % (
        json.dumps(text), json.dumps(os.environ['BUFFER_CHANNEL_ID']))
    response = requests.post('https://api.buffer.com/graphql', headers={
        'Authorization': 'Bearer ' + os.environ['BUFFER_TOKEN'],
        'Content-Type': 'application/json',
    }, json={'query': query}, timeout=60)
    response.raise_for_status()
    data = response.json()
    if data.get('errors'):
        raise RuntimeError('Buffer GraphQL: ' + json.dumps(data['errors'], ensure_ascii=False))
    result = (data.get('data') or {}).get('createPost') or {}
    post_id = (result.get('post') or {}).get('id')
    if not post_id:
        raise RuntimeError('Buffer nie potwierdził dodania posta: ' + str(result.get('message', 'brak id')))
    return post_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', type=date.fromisoformat)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--input', type=Path, help='Lokalna odpowiedź JSON zamiast API')
    args = parser.parse_args()
    day = args.date or datetime.now(WARSAW).date() - timedelta(days=1)
    if day >= datetime.now(WARSAW).date():
        raise ValueError('Wybierz zakończoną dobę.')
    marker = Path('.mix-state') / f'{day}.json'
    if args.publish and marker.exists():
        print('Ten dzień został już dodany do Buffera.')
        return
    payload = json.loads(args.input.read_text()) if args.input else fetch(day)
    energy = aggregate(payload, day)
    text = make_post(energy, day)
    print(text)
    print(f'\nKontrola: {sum(energy.values()) / 1000:.3f} GWh; {len(text)} znaków.')
    if args.publish:
        post_id = publish(text)
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(json.dumps({'date': str(day), 'post_id': post_id, 'text': text}, ensure_ascii=False))
        print('Dodano do kolejki Buffera. ID:', post_id)
    else:
        print('PODGLĄD — nic nie wysłano. Użyj --publish, aby dodać do kolejki.')


if __name__ == '__main__':
    main()
