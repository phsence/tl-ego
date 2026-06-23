import subprocess, json, sys
r = subprocess.run([
    r'H:\unsloth_env\Scripts\python.exe',
    r'C:\Users\Administrator\.openclaw\workspace\song\audio_sensory_analyzer.py',
    r'F:\音乐\R\rammstein\Rammstein.-.[Best.Of].专辑.(mp3).[VeryCD.com]\02-Ich Will.mp3',
    '--json', '--trim', '90'
], capture_output=True, text=True, timeout=120)
d = json.loads(r.stdout)
rh = d.get('rhythm', {})
h = d.get('harmonic', {})
tx = d.get('texture', {})
ti = d.get('timbre', {})
dy = d.get('dynamics', {})
w = d.get('waveform', {})
s = d.get('spectrum', {})

print(f'BPM={rh.get("bpm")} regularity={rh.get("beat_regularity")}')
print(f'Key={h.get("key")} mode={h.get("mode")} confidence={h.get("key_confidence")}%')
print(f'Valence={tx.get("emotional_valence")} Arousal={tx.get("arousal")}')
print(f'Brightness={tx.get("brightness_character")} Density={tx.get("density")}')
print(f'Complexity={tx.get("complexity")} Harmonicity={tx.get("harmonicity")}')
print(f'Dynamic range={dy.get("dynamic_range_db")}dB trend={dy.get("trend")}')
print(f'RMS={w.get("rms_energy")} Crest={w.get("crest_factor")}')
print(f'Centroid={s.get("centroid_hz")}Hz Bandwidth={s.get("bandwidth_hz")}Hz')
ed = s.get('energy_distribution', {})
print(f'Energy: low={ed.get("low_pct")}% mid={ed.get("mid_pct")}% high={ed.get("high_pct")}%')
print(f'Warmth={ti.get("warmth")} Roughness={ti.get("roughness")}')
print(f'Novelty={tx.get("novelty_rate")}')