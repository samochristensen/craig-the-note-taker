import os, wave, threading
from collections import defaultdict

class AudioWriter:
    def __init__(self, session_dir, sample_rate=48000, channels=2, sample_width=2):
        self.session_dir = session_dir
        os.makedirs(self.session_dir, exist_ok=True)
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.locks = defaultdict(threading.Lock)
        self.files = {}

    def _path(self, user_id):
        return os.path.join(self.session_dir, f"user_{user_id}.wav")

    def start_track(self, user_id):
        path = self._path(user_id)
        wf = wave.open(path, 'wb')
        wf.setnchannels(self.channels)
        wf.setsampwidth(self.sample_width)
        wf.setframerate(self.sample_rate)
        self.files[user_id] = wf

    def write(self, user_id, pcm_bytes: bytes):
        if user_id not in self.files:
            self.start_track(user_id)
        with self.locks[user_id]:
            self.files[user_id].writeframes(pcm_bytes)

    def close_all(self):
        for wf in self.files.values():
            wf.close()
        self.files.clear()
