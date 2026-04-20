"""Test TDX Metro APIs directly."""
from dotenv import load_dotenv
import os
import requests
import json

load_dotenv()

# Get TDX token
auth_url = 'https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token'
auth_data = {
    'grant_type': 'client_credentials',
    'client_id': os.getenv('TDX_CLIENT_ID'),
    'client_secret': os.getenv('TDX_CLIENT_SECRET'),
}
token_resp = requests.post(auth_url, data=auth_data)
token = token_resp.json().get('access_token')
headers = {'Authorization': f'Bearer {token}', 'Accept-Encoding': 'gzip'}

print('=' * 60)
print('=== KRTC (Kaohsiung) LiveBoard - Orange Line ===')
print('=' * 60)
url = 'https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/LiveBoard/KRTC'
params = {'$filter': "LineID eq 'O'", '$top': 15, '$format': 'JSON'}
resp = requests.get(url, headers=headers, params=params)
print(f'Status: {resp.status_code}')
data = resp.json()
for item in data[:10]:
    station = item.get('StationName', {}).get('Zh_tw', '?')
    est_time = item.get('EstimateTime')
    head_sign = item.get('TripHeadSign', '')
    dest = item.get('DestinationStationName', {}).get('Zh_tw', '')
    print(f"  {station}: EstimateTime={est_time}, TripHeadSign={head_sign}, Dest={dest}")

print()
print('=' * 60)
print('=== KRTC StationTimeTable ===')
print('=' * 60)
url2 = 'https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/StationTimeTable/KRTC'
params2 = {'$top': 3, '$format': 'JSON'}
resp2 = requests.get(url2, headers=headers, params=params2)
print(f'Status: {resp2.status_code}')
data2 = resp2.json()
for item in data2[:2]:
    station = item.get('StationName', {}).get('Zh_tw', '?')
    timetables = item.get('Timetables', [])
    print(f"  Station: {station}, Timetables count: {len(timetables)}")
    if timetables:
        for tt in timetables[:3]:
            print(f"    - Arrival: {tt.get('ArrivalTime')}, Departure: {tt.get('DepartureTime')}, Dest: {tt.get('DestinationStationName', {}).get('Zh_tw')}")

print()
print('=' * 60)
print('=== TMRT (Taichung) LiveBoard ===')
print('=' * 60)
url_tmrt_live = 'https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/LiveBoard/TMRT'
resp_tmrt = requests.get(url_tmrt_live, headers=headers, params={'$top': 10, '$format': 'JSON'})
print(f'Status: {resp_tmrt.status_code}')
print(f'Response: {resp_tmrt.text[:500] if resp_tmrt.text else "(empty)"}')

print()
print('=' * 60)
print('=== TMRT StationTimeTable (calculated ETA) ===')
print('=' * 60)
from datetime import datetime
url_tmrt_tt = 'https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/StationTimeTable/TMRT'
resp_tmrt_tt = requests.get(url_tmrt_tt, headers=headers, params={'$format': 'JSON'})
print(f'Status: {resp_tmrt_tt.status_code}')
if resp_tmrt_tt.status_code == 200:
    data_tmrt = resp_tmrt_tt.json()
    print(f'Total station records: {len(data_tmrt)}')
    now = datetime.now().strftime('%H:%M')
    print(f'Current time: {now}')
    for item in data_tmrt[:4]:
        station = item.get('StationName', {}).get('Zh_tw', '?')
        direction = item.get('Direction')
        dest_station = item.get('DestinationStationName', {}).get('Zh_tw', '?')
        timetables = item.get('Timetables', [])
        # Find next arrivals after now
        upcoming = [t for t in timetables if t.get('ArrivalTime', '00:00') >= now][:2]
        print(f'Station: {station}, Dir: {direction}, Dest: {dest_station}')
        for t in upcoming:
            arr_time = t.get('ArrivalTime', '?')
            print(f'    Next: {arr_time}')
else:
    print(f'Error: {resp_tmrt_tt.text[:300]}')

# Check S2STravelTime for KRTC as alternative
print()
print('=== KRTC S2STravelTime (for calculating ETA) ===')
url_s2s = 'https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/S2STravelTime/KRTC'
resp_s2s = requests.get(url_s2s, headers=headers, params={'$top': 3, '$format': 'JSON'})
print(f'Status: {resp_s2s.status_code}')
data_s2s = resp_s2s.json()
for item in data_s2s[:2]:
    travel_times = item.get('TravelTimes', [])
    print(f"  Line: {item.get('LineID')}, Direction: {item.get('Direction')}, Segments: {len(travel_times)}")
    for tt in travel_times[:3]:
        print(f"    {tt.get('FromStationName', {}).get('Zh_tw')} -> {tt.get('ToStationName', {}).get('Zh_tw')}: {tt.get('RunTimeSecs')}s + {tt.get('StopTimeSecs')}s stop")

# Test Frequency API
print()
print('=== KRTC Frequency ===')
url_freq = 'https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/Frequency/KRTC'
resp_freq = requests.get(url_freq, headers=headers, params={'$top': 3, '$format': 'JSON'})
print(f'Status: {resp_freq.status_code}')
data_freq = resp_freq.json()
for item in data_freq[:2]:
    headways = item.get('Headways', [])
    print(f"  Line: {item.get('LineID')}, Headways: {len(headways)}")
    for hw in headways[:2]:
        print(f"    {hw.get('StartTime')}-{hw.get('EndTime')}: {hw.get('MinHeadwayMins')}-{hw.get('MaxHeadwayMins')} min")

print()
print('=' * 60)
print('=== TRTC (Taipei) StationTimeTable - Sample ===')
print('=' * 60)
url4 = 'https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/StationTimeTable/TRTC'
resp4 = requests.get(url4, headers=headers, params={'$top': 2, '$format': 'JSON'})
print(f'Status: {resp4.status_code}')
data4 = resp4.json()
for item in data4[:1]:
    station = item.get('StationName', {}).get('Zh_tw', '?')
    timetables = item.get('Timetables', [])
    print(f"  Station: {station}, Timetables count: {len(timetables)}")
    if timetables:
        for tt in timetables[:5]:
            print(f"    - Seq: {tt.get('Sequence')}, Arr: {tt.get('ArrivalTime')}, Dep: {tt.get('DepartureTime')}, Dest: {tt.get('DestinationStationName', {}).get('Zh_tw')}")
