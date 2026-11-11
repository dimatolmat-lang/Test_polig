import wave, numpy as np
with wave.open("Angle_0/data_2026-11-11_13-32-26.wav", "rb") as wf:
    n_ch = wf.getnchannels()
    fs   = wf.getframerate()
    raw  = wf.readframes(wf.getnframes())
data = np.frombuffer(raw, dtype=np.int16).reshape(-1, n_ch)
print(f"Каналов: {n_ch}, частота: {fs}, форма: {data.shape}")
print(f"Длительность: {data.shape[0] / fs:.3f} с")
print("RMS по каналам:", np.sqrt(np.mean(data.astype(np.float64)**2, axis=0)))