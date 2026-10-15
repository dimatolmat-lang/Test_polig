# -*- coding: utf-8 -*-
"""
Генератор и проигрыватель тестового сигнала (tkinter).

1. Ввод угла: -90...90, шаг 1, по умолчанию 0.
2. Источник звука: WAV-файл или генерация.
3. Кнопка «СТАРТ» (зелёная) -> «СТОП» (синяя, заполняется как «хот-бар»).
4. Последовательность генерации — см. build_test_signal().
5. Хуки для нашей функций: user_hook_on_start() / user_hook_on_stop().
6. Папка «Угол_<N>» создаётся рядом со скриптом.
7. Лог (время начала, описание, время конца) пишется в эту папку.
8. Сгенерированный сигнал сохраняется там же как «Исходник.wav».

Полная генерация ~64 минуты, «Исходник.wav» ~320 МБ (моно/16 бит/44100 Гц).

Зависимости:  pip install numpy sounddevice   (Python 3.7+)
"""

import bisect
import datetime
import os
import queue
import threading
import time
import wave

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import numpy as np
    import sounddevice as sd
except ImportError as _err:
    _tmp = tk.Tk()
    _tmp.withdraw()
    messagebox.showerror(
        "Не установлены зависимости",
        "Установите библиотеки командой:\n\n"
        "    pip install numpy sounddevice\n\n"
        f"Подробности: {_err}",
    )
    raise SystemExit(1)


# ----------------------------- ПАРАМЕТРЫ ГЕНЕРАЦИИ -----------------------------
SAMPLE_RATE = 44100               # Гц (поставьте 22050 — файл будет вдвое меньше)
FREQ_START = 100                  # начальная частота, Гц
FREQ_END = 2000                   # конечная частота, Гц
FREQ_STEP = 10                    # шаг частоты, Гц
TONE_DURATION = 4.0               # длительность тона, с
PAUSE_DURATION = 1.0              # длительность паузы, с
SWEEP_DURATION = 4.0              # длительность свипа, с
POWER_LEVELS = (25, 50, 75, 100)  # уровни, %
# «Мощность» здесь = доля амплитуды (power/100). Если нужен физический смысл
# (мощность ~ амплитуда^2), замените в build_test_signal (power / 100.0)
# на ((power / 100.0) ** 0.5).


class AbortedError(Exception):
    """Воспроизведение остановлено пользователем."""


# ==============================================================================
#                                ГЕНЕРАЦИЯ СИГНАЛА
# ==============================================================================
def build_test_signal(stop_flag=None):
    """
    Строит тестовый сигнал согласно ТЗ (п.4).

    Возвращает:
        buf      — np.ndarray[int16], моно;
        segments — список (начало_с, длительность_с, описание) для лога/статуса;
        total    — общая длительность, с.
    """
    n_freq = len(range(FREQ_START, FREQ_END + 1, FREQ_STEP))
    n_pause = int(round(PAUSE_DURATION * SAMPLE_RATE))
    n_tone = int(round(TONE_DURATION * SAMPLE_RATE))
    n_sweep = int(round(SWEEP_DURATION * SAMPLE_RATE))
    n_total = ((1 + (n_freq + 1) * len(POWER_LEVELS)) * n_pause
               + n_freq * len(POWER_LEVELS) * n_tone
               + len(POWER_LEVELS) * n_sweep)

    buf = np.zeros(n_total, dtype=np.int16)
    segments = []
    pos = 0

    def add(arr, desc):
        nonlocal pos
        n = len(arr)
        segments.append((pos / SAMPLE_RATE, n / SAMPLE_RATE, desc))
        buf[pos:pos + n] = arr
        pos += n

    def check_stop():
        if stop_flag is not None and stop_flag.is_set():
            raise AbortedError()

    silence = np.zeros(n_pause, dtype=np.int16)
    t_tone = np.arange(n_tone, dtype=np.float64) / SAMPLE_RATE

    # форма свипа 100->2000->100 Гц (общая для всех уровней)
    half = n_sweep // 2
    f_inst = np.concatenate((
        np.linspace(FREQ_START, FREQ_END, half),
        np.linspace(FREQ_END, FREQ_START, n_sweep - half)))
    sweep_wave = np.sin(2.0 * np.pi * np.cumsum(f_inst) / SAMPLE_RATE)

    # 4.1 — начальная пауза
    add(silence, f"Пауза {PAUSE_DURATION:.0f} с")

    # 4.2...4.X — ступени по частоте 100..2000 Гц с шагом 10 Гц
    for freq in range(FREQ_START, FREQ_END + 1, FREQ_STEP):
        for power in POWER_LEVELS:
            check_stop()
            amp = (power / 100.0) * 32767.0
            add((amp * np.sin(2.0 * np.pi * freq * t_tone)).astype(np.int16),
                f"Синус {freq} Гц, мощность {power} %, {TONE_DURATION:.0f} с")
            add(silence, f"Пауза {PAUSE_DURATION:.0f} с")

    # 4.X1...4.X3 — свипы 100->2000->100 Гц на 25/50/75/100 %
    for power in POWER_LEVELS:
        check_stop()
        amp = (power / 100.0) * 32767.0
        add((amp * sweep_wave).astype(np.int16),
            f"Свип {FREQ_START}->{FREQ_END}->{FREQ_START} Гц (линейно), "
            f"мощность {power} %, {SWEEP_DURATION:.0f} с")
        add(silence, f"Пауза {PAUSE_DURATION:.0f} с")

    return buf[:pos], segments, pos / SAMPLE_RATE


def generation_info():
    """(длительность_с, размер_wav_МБ) генерации по текущим параметрам."""
    n_freq = len(range(FREQ_START, FREQ_END + 1, FREQ_STEP))
    total = (PAUSE_DURATION
             + n_freq * len(POWER_LEVELS) * (TONE_DURATION + PAUSE_DURATION)
             + len(POWER_LEVELS) * (SWEEP_DURATION + PAUSE_DURATION))
    size_mb = total * SAMPLE_RATE * 2 / (1024.0 * 1024.0)
    return total, size_mb


# ==============================================================================
#                                     WAV I/O
# ==============================================================================
def load_wav(path):
    """Читает PCM WAV. Возвращает (данные float32 [N]/[N,каналы], Гц, длительность_с)."""
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        fs = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sampwidth == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v[v >= (1 << 23)] -= (1 << 24)
        data = v.astype(np.float32) / float(1 << 23)
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Неподдерживаемая разрядность WAV: {sampwidth * 8} бит")

    if n_channels > 1:
        data = data.reshape(-1, n_channels)
    return data, fs, data.shape[0] / fs


def save_wav_int16(path, buf, fs, stop_flag=None, chunk=1 << 20):
    """Сохраняет int16-моно буфер в WAV по частям (без раздувания памяти)."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(fs)
        for i in range(0, len(buf), chunk):
            if stop_flag is not None and stop_flag.is_set():
                raise AbortedError()
            wf.writeframes(buf[i:i + chunk].tobytes())


# ==============================================================================
#                            КНОПКА «ХОТ-БАР» (п.3)
# ==============================================================================
class HotButton(tk.Canvas):
    """Покой: зелёная, «СТАРТ». Активна: синяя, «СТОП», заполняется прогрессом."""

    COLOR_IDLE = "#2ea44f"     # зелёный
    COLOR_ACTIVE = "#1f6feb"   # синий
    COLOR_FILL = "#79b8ff"     # заливка прогресса
    COLOR_TEXT = "#ffffff"

    def __init__(self, master, command, width=460, height=64):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bw, self._bh = width, height   # ВАЖНО: не self._w — занят tkinter'ом!
        self._command = command
        self._active = False
        self._progress = 0.0
        self.bind("<Button-1>", self._on_click)
        self._redraw()

    def _on_click(self, _event):
        if self._command:
            self._command()

    def set_active(self, active):
        self._active = bool(active)
        if not self._active:
            self._progress = 0.0
        self._redraw()

    def set_progress(self, fraction):
        self._progress = max(0.0, min(1.0, float(fraction)))
        if self._active:
            self._redraw()

    def _redraw(self):
        self.delete("all")
        base = self.COLOR_ACTIVE if self._active else self.COLOR_IDLE
        self.create_rectangle(0, 0, self._bw, self._bh, fill=base, outline="")
        if self._active and self._progress > 0.0:
            fill_w = int(self._bw * self._progress)
            self.create_rectangle(0, 0, fill_w, self._bh,
                                  fill=self.COLOR_FILL, outline="")
        self.create_text(self._bw // 2, self._bh // 2,
                         text="СТОП" if self._active else "СТАРТ",
                         fill=self.COLOR_TEXT, font=("Arial", 15, "bold"))


# ==============================================================================
#                              ОСНОВНОЕ ПРИЛОЖЕНИЕ
# ==============================================================================
class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Генератор тестового сигнала")
        self.resizable(False, False)

        # ---------- служебное состояние ----------
        self._active = False                 # идёт подготовка/воспроизведение
        self._stop_flag = threading.Event()  # взводится кнопкой «СТОП»
        self._worker = None
        self._ui_queue = queue.Queue()       # статусы: рабочий поток -> GUI
        self._done_flag = False
        self._done_reason = None             # "finished" | "manual" | "error"
        self._error_text = None
        self._playback_started = False
        self._play_t0 = None                 # time.monotonic() начала звука
        self._total_duration = None
        self._segments = None                # расписание сегментов генерации
        self._seg_offsets = None
        self._stop_hook_called = False
        self._click_guard_until = 0.0

        # ---------- данные текущего запуска (видны в хуках) ----------
        self._last_angle = None
        self._last_folder = None
        self._last_log = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        frm = ttk.Frame(self, padding=(14, 10))
        frm.grid(sticky="nsew")
        frm.columnconfigure(2, weight=1)

        # ---- 1. Угол ----
        ttk.Label(frm, text="Угол, °:").grid(row=0, column=0, sticky="w", pady=4)
        self._angle_var = tk.StringVar(value="0")
        self._angle_spin = ttk.Spinbox(frm, from_=-90, to=90, increment=1,
                                       textvariable=self._angle_var, width=6,
                                       command=self._sync_angle)
        self._angle_spin.grid(row=0, column=1, sticky="w", padx=(6, 12), pady=4)
        self._angle_spin.bind("<KeyRelease>", lambda _e: self._sync_angle())
        self._angle_scale = ttk.Scale(frm, from_=-90, to=90, value=0,
                                      command=self._on_scale)
        self._angle_scale.grid(row=0, column=2, sticky="ew", pady=4)

        # ---- 2. Источник звука ----
        src = ttk.LabelFrame(frm, text="Источник звука", padding=(10, 6))
        src.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self._source_var = tk.StringVar(value="gen")
        ttk.Radiobutton(src, text="WAV-файл", variable=self._source_var, value="wav",
                        command=self._update_source_ui).pack(side="left")
        ttk.Radiobutton(src, text="Генерация", variable=self._source_var, value="gen",
                        command=self._update_source_ui).pack(side="left", padx=(14, 0))
        self._wav_path_var = tk.StringVar()
        self._wav_entry = ttk.Entry(src, textvariable=self._wav_path_var, width=40)
        self._wav_entry.pack(side="left", padx=(14, 0))
        self._browse_btn = ttk.Button(src, text="Обзор…", width=9,
                                       command=self._browse_wav)
        self._browse_btn.pack(side="left", padx=(8, 0))

        # информация о генерации
        self._info_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self._info_var, foreground="#555",
                  width=80, anchor="w").grid(row=2, column=0, columnspan=3,
                                             sticky="w", padx=4, pady=(0, 6))

        # ---- 3. Кнопка СТАРТ/СТОП ----
        self._hotbtn = HotButton(frm, command=self._on_hotbutton)
        self._hotbtn.grid(row=3, column=0, columnspan=3, pady=(2, 8))

        # ---- 4. Статус ----
        self._status_var = tk.StringVar(value="Готов к работе")
        ttk.Label(frm, textvariable=self._status_var, anchor="w", justify="left",
                  wraplength=620).grid(row=4, column=0, columnspan=3,
                                       sticky="w", padx=4, pady=(2, 4))

        self._update_source_ui()

    # ------------------------------------------------------------- угол ----
    def _on_scale(self, value):
        v = int(round(float(value)))
        if abs(float(value) - v) > 1e-6:
            self._angle_scale.set(v)          # «прилипание» к целым
        self._angle_var.set(str(v))

    def _sync_angle(self):
        try:
            v = int(self._angle_spin.get())
        except (ValueError, tk.TclError):
            return
        self._angle_scale.set(max(-90, min(90, v)))

    def _get_angle(self):
        try:
            v = int(float(self._angle_spin.get()))
        except (ValueError, tk.TclError):
            v = 0
        return max(-90, min(90, v))

    # ---------------------------------------------------------- источник ----
    def _update_source_ui(self):
        is_wav = self._source_var.get() == "wav"
        state = "normal" if is_wav else "disabled"
        self._wav_entry.configure(state=state)
        self._browse_btn.configure(state=state)
        if is_wav:
            self._info_var.set("")
        else:
            total, size_mb = generation_info()
            self._info_var.set(f"Генерация: {self._fmt_time(total)} "
                               f"(~{size_mb:.0f} МБ, {FREQ_START}–{FREQ_END} Гц, шаг {FREQ_STEP} Гц)")

    def _browse_wav(self):
        path = filedialog.askopenfilename(
            title="Выбор WAV-файла",
            filetypes=[("WAV-файлы", "*.wav *.wave"), ("Все файлы", "*.*")])
        if path:
            self._wav_path_var.set(path)

    # ---------------------------------------------------- СТАРТ / СТОП ----
    def _on_hotbutton(self):
        if self._active:
            if time.monotonic() < self._click_guard_until:
                return                         # защита от двойного клика
            self._stop_playback()
        else:
            self._start()

    def _stop_playback(self):
        self._stop_flag.set()
        try:
            sd.stop()                          # прервать текущее воспроизведение
        except Exception:
            pass
        self._status_var.set("Остановка…")

    def _start(self):
        mode = self._source_var.get()
        wav_path = self._wav_path_var.get().strip()

        if mode == "wav":
            if not wav_path:
                messagebox.showwarning("Нет файла", "Выберите WAV-файл (кнопка «Обзор…»).")
                return
            if not os.path.isfile(wav_path):
                messagebox.showwarning("Файл не найден", f"Файл не найден:\n{wav_path}")
                return

        angle = self._get_angle()

        # ---- 6. папка «Угол_<N>» рядом с программой ----
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            base_dir = os.getcwd()
        folder = os.path.join(base_dir, f"Угол_{angle}")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось создать папку:\n{folder}\n\n{exc}")
            return

        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = os.path.join(folder, f"Лог_{stamp}.txt")
        source_wav = os.path.join(folder, "Исходник.wav")

        # данные текущего запуска (используются в хуках)
        self._last_angle = angle
        self._last_folder = folder
        self._last_log = log_path

        # ==================================================================
        # ХУК СТАРТ (п.5 ТЗ): вызов вашей функции при старте.
        # Сама функция — ниже, метод user_hook_on_start().
        # ==================================================================
        try:
            self.user_hook_on_start()
        except Exception as exc:
            messagebox.showerror("Ошибка в функции старта", str(exc))
            self._status_var.set("Готов к работе")
            return
        # ==================================================================

        # ---- переход в активное состояние ----
        self._active = True
        self._stop_flag.clear()
        self._done_flag = False
        self._done_reason = None
        self._error_text = None
        self._playback_started = False
        self._play_t0 = None
        self._total_duration = None
        self._segments = None
        self._seg_offsets = None
        self._stop_hook_called = False
        self._click_guard_until = time.monotonic() + 0.4

        self._hotbtn.set_active(True)
        self._hotbtn.set_progress(0.0)
        self._status_var.set("Подготовка…")

        self._worker = threading.Thread(target=self._worker_run,
                                        args=(mode, angle, folder, log_path,
                                              wav_path, source_wav),
                                        daemon=True)
        self._worker.start()
        self.after(100, self._poll)

    # ============================== РАБОЧИЙ ПОТОК ==========================
    def _worker_run(self, mode, angle, folder, log_path, wav_path, source_wav):
        log = None
        reason = "finished"
        error_text = None

        def status(text):
            self._ui_queue.put(("status", text))

        try:
            start_dt = datetime.datetime.now()
            log = open(log_path, "w", encoding="utf-8")
            log.write("=" * 72 + "\n")
            log.write("ЛОГ ЗАПУСКА\n")
            log.write(f"Дата/время начала : {start_dt:%Y-%m-%d %H:%M:%S}\n")
            log.write(f"Угол              : {angle}\n")
            log.write(f"Режим             : {'WAV-файл' if mode == 'wav' else 'Генерация'}\n")
            log.write(f"Папка             : {folder}\n")
            if mode == "wav":
                log.write(f"WAV-файл          : {wav_path}\n")
                log.write(f"[{start_dt:%H:%M:%S}] СТАРТ: воспроизведение WAV-файла\n")
                log.write(f"Имя файла: {os.path.basename(wav_path)}\n")
                log.write(f"Путь:     {wav_path}\n")
            else:
                log.write(f"[{start_dt:%H:%M:%S}] СТАРТ: генерация тестового сигнала\n")
            log.write("=" * 72 + "\n\n")
            log.flush()

            # ---------- подготовка данных ----------
            if mode == "wav":
                status("Чтение WAV-файла…")
                data, fs, total = load_wav(wav_path)
                log.write(f"Длительность: {total:.1f} с\n\n")
            else:
                status("Генерация сигнала (несколько секунд)…")
                buf, segments, total = build_test_signal(self._stop_flag)

                if self._stop_flag.is_set():
                    raise AbortedError()

                # ---- 8. сохранение «Исходник.wav» ----
                status("Сохранение «Исходник.wav»…")
                try:
                    save_wav_int16(source_wav, buf, SAMPLE_RATE, self._stop_flag)
                except AbortedError:
                    try:
                        os.remove(source_wav)     # удалить недописанный файл
                    except OSError:
                        pass
                    raise
                log.write(f"[{datetime.datetime.now():%H:%M:%S}] "
                          f"Сигнал сгенерирован и сохранён: {source_wav}\n")
                log.write(f"Длительность: {total:.1f} с\n\n")
                log.flush()

                data, fs = buf, SAMPLE_RATE
                self._segments = segments
                self._seg_offsets = [s[0] for s in segments]

            # Сюда можно перенести вызов self.user_hook_on_start(), если ваша
            # функция должна выполняться непосредственно перед началом звука.

            if self._stop_flag.is_set():
                raise AbortedError()

            # ---- 7. расписание генерации в лог ----
            play_dt = datetime.datetime.now()
            if self._segments:
                log.write(f"[{play_dt:%H:%M:%S}] Начало воспроизведения. Расписание:\n")
                for off, _dur, desc in self._segments:
                    seg_dt = play_dt + datetime.timedelta(seconds=off)
                    log.write(f"  [{seg_dt:%H:%M:%S}] {desc}\n")
                log.write("\n")
                log.flush()

            # ---- воспроизведение ----
            self._total_duration = total
            self._play_t0 = time.monotonic()
            self._playback_started = True

            sd.play(data, fs)
            if self._stop_flag.is_set():          # защита от гонки «СТОП раньше звука»
                sd.stop()
            sd.wait()

            if self._stop_flag.is_set():
                reason = "manual"

        except AbortedError:
            reason = "manual"
        except Exception as exc:
            reason = "error"
            error_text = f"{type(exc).__name__}: {exc}"
            try:
                sd.stop()
            except Exception:
                pass
        finally:
            end_dt = datetime.datetime.now()
            if log is not None:
                try:
                    if reason == "finished":
                        log.write(f"\n[{end_dt:%H:%M:%S}] КОНЕЦ: воспроизведение завершено\n")
                    elif reason == "manual":
                        log.write(f"\n[{end_dt:%H:%M:%S}] КОНЕЦ: остановлено пользователем\n")
                    else:
                        log.write(f"\n[{end_dt:%H:%M:%S}] КОНЕЦ: ошибка — {error_text}\n")
                    log.write(f"Дата/время конца : {end_dt:%Y-%m-%d %H:%M:%S}\n")
                except Exception:
                    pass
                finally:
                    try:
                        log.close()
                    except Exception:
                        pass
            self._done_reason = reason
            self._error_text = error_text
            self._done_flag = True               # подхватывается потоком GUI

    # ============================ ОПРОС (GUI-поток) ========================
    def _poll(self):
        # сообщения от рабочего потока
        try:
            while True:
                kind, text = self._ui_queue.get_nowait()
                if kind == "status":
                    self._status_var.set(text)
        except queue.Empty:
            pass

        if self._done_flag:
            self._finish()
            return

        if self._playback_started and self._total_duration:
            elapsed = time.monotonic() - self._play_t0
            frac = min(1.0, elapsed / self._total_duration)
            self._hotbtn.set_progress(frac)      # «хот-бар»
            self._status_var.set(
                f"Воспроизведение: {self._fmt_time(elapsed)} / "
                f"{self._fmt_time(self._total_duration)}{self._current_segment(elapsed)}")

        self.after(100, self._poll)

    def _current_segment(self, elapsed):
        if not self._seg_offsets:
            return "  •  WAV-файл"
        i = bisect.bisect_right(self._seg_offsets, elapsed) - 1
        if 0 <= i < len(self._segments):
            off, dur, desc = self._segments[i]
            if elapsed < off + dur:
                return f"  •  {desc}"
        return ""

    def _finish(self):
        self._active = False
        self._playback_started = False
        self._worker = None
        self._hotbtn.set_active(False)

        reason = self._done_reason or "finished"
        if reason == "error":
            self._status_var.set(f"Ошибка: {self._error_text}")
            messagebox.showerror("Ошибка", self._error_text or "Неизвестная ошибка")
        elif reason == "manual":
            self._status_var.set("Остановлено пользователем")
        else:
            self._status_var.set("Воспроизведение завершено")

        # ХУК СТОП (п.5 ТЗ) — см. метод user_hook_on_stop() ниже.
        self._call_stop_hook(reason)

    def _call_stop_hook(self, reason):
        if self._stop_hook_called:
            return
        self._stop_hook_called = True
        try:
            self.user_hook_on_stop(reason)
        except Exception as exc:
            messagebox.showerror("Ошибка в функции остановки", str(exc))

    @staticmethod
    def _fmt_time(seconds):
        seconds = max(0, int(round(seconds)))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _on_close(self):
        if self._active:
            if not messagebox.askyesno("Выход",
                                       "Воспроизведение ещё не завершено.\nОстановить и выйти?"):
                return
            self._stop_flag.set()
            try:
                sd.stop()
            except Exception:
                pass
            if self._worker is not None:
                self._worker.join(timeout=5)
            self._call_stop_hook(self._done_reason or "manual")
        self.destroy()

    # ######################################################################
    # #############  МЕСТО ДЛЯ ВАШЕЙ ФУНКЦИИ ПРИ СТАРТЕ  (п.5)  ############
    # ######################################################################
    # Вызывается при нажатии кнопки «СТАРТ», в потоке GUI, сразу после
    # создания папки «Угол_<N>» и подготовки лога (до начала звука).
    #
    # Доступные данные:
    #   self._last_angle  — угол (int, -90..90)
    #   self._last_folder — путь к папке «Угол_<N>»
    #   self._last_log    — путь к лог-файлу
    # ######################################################################
    def user_hook_on_start(self):
        # ---- ВСТАВЬТЕ ВАШ КОД ЗДЕСЬ ----
        # Пример:
        # my_start_function(self._last_angle, self._last_folder)
        pass

    # ######################################################################
    # ###########  МЕСТО ДЛЯ ВАШЕЙ ФУНКЦИИ ПРИ ОСТАНОВКЕ  (п.5)  ############
    # ######################################################################
    # Вызывается при остановке: либо вручную (кнопка «СТОП»), либо
    # автоматически после окончания воспроизведения, либо при ошибке.
    #
    # reason: "manual"   — нажата кнопка «СТОП»;
    #         "finished" — воспроизведение закончилось само;
    #         "error"    — произошла ошибка.
    # ######################################################################
    def user_hook_on_stop(self, reason):
        # ---- ВСТАВЬТЕ ВАШ КОД ЗДЕСЬ ----
        # Пример:
        # my_stop_function(self._last_angle, self._last_folder)
        pass


if __name__ == "__main__":
    App().mainloop()