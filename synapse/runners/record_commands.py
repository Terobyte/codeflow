"""№5 (Р-7): records 30-50 voice command phrases one at a time (Enter to start, Enter to
stop), 16kHz mono WAV, with a JSON manifest ({phrase, file, bg, ts}). Uses sounddevice/
soundfile (extra `record`) — imported lazily inside `record_session()` so importing this
module doesn't require the `record` extra unless recording is actually invoked.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read_phrases(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _manifest_path(out_dir: Path) -> Path:
    return out_dir / "manifest.json"


def _load_manifest(out_dir: Path) -> list[dict]:
    path = _manifest_path(out_dir)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(out_dir: Path, entries: list[dict]) -> None:
    _manifest_path(out_dir).write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def record_session(phrases_path: str, out_dir: str, bg: str, resume: bool) -> int:
    import threading
    import time

    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    phrases = _read_phrases(phrases_path)
    manifest = _load_manifest(out)
    done_phrases = {entry["phrase"] for entry in manifest if entry.get("bg") == bg} if resume else set()

    sample_rate = 16000
    for idx, phrase in enumerate(phrases, start=1):
        if phrase in done_phrases:
            continue
        print(f"[{idx}/{len(phrases)}] Фраза: {phrase!r} (фон: {bg})")
        input("Нажми Enter, чтобы начать запись...")
        recording: list = []
        stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16")
        stream.start()
        print("Идёт запись... нажми Enter, чтобы остановить.")
        stop_event = threading.Event()

        def _wait_for_enter() -> None:
            input()
            stop_event.set()

        # B-CORE-2: вейтер заводится ДО try, иначе упавший конструктор потока оставил бы
        # finally с NameError вместо честной ошибки.
        waiter = threading.Thread(target=_wait_for_enter, daemon=True)
        try:
            waiter.start()
            while not stop_event.is_set():
                data, _ = stream.read(sample_rate // 10)
                recording.append(data.copy())
        finally:
            stream.stop()
            stream.close()
            # B-CORE-2: дождаться вейтера, а не бросить его. Поток спавнится НА КАЖДУЮ фразу,
            # и на штатном пути он уже возвращается (input() отдал Enter → stop_event) — join
            # лишь честно это подтверждает. Если же stream.read() бросил, вейтер остался висеть
            # в input(): его join не добудится (прервать input() в Python нечем) и он уйдёт
            # драться за stdin со следующей фразой. Таймаут держит цикл живым; полное лечение
            # требует убрать input() из потока — за рамками этого бага.
            waiter.join(timeout=1.0)

        audio = np.concatenate(recording, axis=0) if recording else np.zeros((0, 1), dtype="int16")
        filename = f"{idx:03d}_{bg}.wav"
        filepath = out / filename
        sf.write(str(filepath), audio, sample_rate)
        manifest.append({"phrase": phrase, "file": filename, "bg": bg, "ts": time.time()})
        _save_manifest(out, manifest)
        print(f"Сохранено: {filepath}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synapse №5: record voice commands (Р-7)")
    parser.add_argument("--phrases", required=True, help="Text file, one phrase per line.")
    parser.add_argument("--out", required=True, help="Output directory for WAVs + manifest.json.")
    parser.add_argument("--bg", required=True, choices=["тихая", "улица", "машина"], help="Background noise condition.")
    parser.add_argument("--resume", action="store_true", help="Skip phrases already in the manifest.")
    args = parser.parse_args(argv)
    return record_session(args.phrases, args.out, args.bg, args.resume)


if __name__ == "__main__":
    raise SystemExit(main())
