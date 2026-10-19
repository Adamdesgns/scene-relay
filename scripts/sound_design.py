"""Create original sparse nonmusical UI clicks and noise swells, using no assets."""
from array import array
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import wave


def create_sound(output: Path, duration: float, cues: list[float]) -> dict:
    if output.exists():
        raise ValueError("Output already exists; choose a new path to preserve it.")
    if not 1 <= duration <= 180 or any(not 0 <= cue < duration for cue in cues):
        raise ValueError("Duration or cue is outside the supported range.")
    rate = 48000
    count = round(duration * rate)
    left, right = array('f', [0]) * count, array('f', [0]) * count
    rng = random.Random(20261018)
    for index, cue in enumerate(cues):
        # A brief filtered-noise click with a subtle, unequal stereo position.
        # Transitions get a short rising noise envelope: no pitches or melody.
        pan = (-.22, .18, -.10, .24)[index % 4]
        gains = (math.sqrt((1 - pan) / 2), math.sqrt((1 + pan) / 2))
        start = round(cue * rate)
        length = round((.10 if index % 3 == 0 else .065) * rate)
        low = previous = 0.
        for offset in range(length):
            at = start + offset
            if at >= count:
                break
            noise = rng.uniform(-1., 1.)
            low += .18 * (noise - low)
            bright = noise - low
            transient = bright * math.exp(-offset / (rate * .009))
            soft = low * math.exp(-offset / (rate * .027))
            attack = min(1., offset / (rate * .0015))
            value = (transient * .20 + soft * .13) * attack
            left[at] += value * gains[0]
            right[at] += value * gains[1]
        if index in {1, 3, 5, 6}:
            # Aperiodic filtered air sweep ends immediately before the click.
            length = round(.28 * rate)
            start = max(0, round((cue - .29) * rate))
            low = 0.
            for offset in range(length):
                at = start + offset
                if at >= count:
                    break
                progress = offset / max(1, length - 1)
                noise = rng.uniform(-1., 1.)
                low += (.04 + .14 * progress) * (noise - low)
                envelope = math.sin(math.pi * progress) ** 2
                value = low * envelope * .10
                left[at] += value * gains[1]
                right[at] += value * gains[0]
    peak = max(max(abs(v) for v in left), max(abs(v) for v in right), 1e-12)
    scale = min(1., 10 ** (-12 / 20) / peak)
    pcm = array('h')
    energy = 0.
    for lval, rval in zip(left, right):
        lval, rval = lval * scale, rval * scale
        energy += lval*lval + rval*rval
        pcm.extend((round(max(-1, min(1, lval)) * 32767), round(max(-1, min(1, rval)) * 32767)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), 'wb') as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    with output.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {"file": output.name, "duration": duration, "sample_rate": rate, "channels": 2,
            "cues": cues, "kind": "original deterministic nonmusical UI noise transients",
            "peak_dbfs": round(20 * math.log10(peak * scale), 2),
            "rms_dbfs": round(10 * math.log10(max(energy / (count * 2), 1e-15)), 2),
            "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=40.)
    parser.add_argument('--cues', default='.4,4,8.5,14,19.5,25.5,33,37')
    args = parser.parse_args()
    print(json.dumps(create_sound(args.output, args.duration, [float(cue) for cue in args.cues.split(',')]), indent=2))


if __name__ == '__main__':
    main()
