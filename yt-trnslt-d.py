import sys
import subprocess
import os
import re
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit, QFileDialog, QLabel, QHBoxLayout, QSpinBox, QCheckBox, QProgressBar, QComboBox
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QMutex, QMutexLocker, QObject
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

class QualityWorker(QThread):
    quality_signal = pyqtSignal(list)
    finished_signal = pyqtSignal()
    log_signal = pyqtSignal(str)

    def __init__(self, playlist_url):
        super().__init__()
        self.playlist_url = playlist_url
        self.quality_cache = {}

    def get_available_qualities(self, playlist_url):
        self.log_signal.emit(f"Starting quality check for {playlist_url}")
        try:
            if playlist_url in self.quality_cache:
                self.log_signal.emit(f"Returning cached qualities for {playlist_url}: {self.quality_cache[playlist_url]}")
                return self.quality_cache[playlist_url]

            result = subprocess.run(["yt-dlp", "--flat-playlist", "--get-url", playlist_url], capture_output=True, text=True)
            video_urls = result.stdout.strip().split("\n")
            if not video_urls or result.returncode != 0:
                self.log_signal.emit(f"Failed to get video URLs: {result.stderr}")
                return ["best"]

            for i, video_url in enumerate(video_urls[:3]):
                self.log_signal.emit(f"Checking formats for video {i+1}: {video_url}")
                formats_result = subprocess.run(["yt-dlp", "-F", video_url], capture_output=True, text=True, timeout=15)
                if formats_result.returncode == 0:
                    formats = formats_result.stdout.splitlines()
                    self.log_signal.emit(f"Raw formats output for {video_url}: {formats}")
                    qualities = set()
                    for line in formats[2:]:
                        match = re.search(r'(\d+p(?:\d+)?)', line)
                        if match:
                            quality = match.group(1)
                            self.log_signal.emit(f"Found quality: {quality}")
                            qualities.add(quality)
                        else:
                            self.log_signal.emit(f"No match for line: {line}")
                    if qualities:
                        qualities_list = ["best"] + sorted(list(qualities), reverse=True)
                        self.quality_cache[playlist_url] = qualities_list
                        self.log_signal.emit(f"Available qualities: {qualities_list}")
                        return qualities_list
                    else:
                        self.log_signal.emit(f"No qualities found for {video_url}")
                else:
                    self.log_signal.emit(f"yt-dlp -F failed for {video_url}: {formats_result.stderr}")
            self.log_signal.emit("No valid video found with available qualities")
            return ["best"]
        except subprocess.TimeoutExpired:
            self.log_signal.emit(f"Timeout expired while getting qualities for {playlist_url}: {formats_result.stdout if 'formats_result' in locals() else 'No output'}")
            return ["best"]
        except Exception as e:
            self.log_signal.emit(f"Error in get_available_qualities: {str(e)}")
            return ["best"]

    def run(self):
        qualities = self.get_available_qualities(self.playlist_url)
        self.quality_signal.emit(qualities)
        self.finished_signal.emit()

class VideoProcessor(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(str, int, int)  # (playlist_url, processed, total)
    finished_signal = pyqtSignal(str, int)  # (video_url, index)

    def __init__(self, video_url, index, total, save_path, volume_ratio, keep_original_audio, video_quality):
        super().__init__()
        self.video_url = video_url
        self.index = index
        self.total = total
        self.save_path = save_path
        self.volume_ratio = volume_ratio
        self.keep_original_audio = keep_original_audio
        self.video_quality = video_quality
        self.script_path = os.path.join(os.path.dirname(__file__), "translate.ps1")
        self.proc = None

    def run(self):
        try:
            self.log_signal.emit(f"Processing video {self.index} of {self.total}: {self.video_url}")
            process = ["pwsh", "-File", self.script_path, self.video_url, str(self.volume_ratio), "--output-dir", self.save_path, "--quality", self.video_quality]
            if self.keep_original_audio:
                process.append("--keep-original")
            else:
                process.append("--replace-audio")

            self.proc = subprocess.Popen(process, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, universal_newlines=True)
            for line in self.proc.stdout:
                self.log_signal.emit(line.strip())

            stderr_output = self.proc.stderr.read()
            if stderr_output:
                self.log_signal.emit("PowerShell stderr:")
                self.log_signal.emit(stderr_output)

            if self.proc.returncode != 0:
                self.log_signal.emit(f"Error processing video {self.index} ({self.video_url}): PowerShell exited with code {self.proc.returncode}")
            else:
                self.log_signal.emit(f"Successfully processed video {self.index} of {self.total}: {self.video_url}")

        except Exception as e:
            self.log_signal.emit(f"Critical error processing video {self.index} ({self.video_url}): {str(e)}")
        finally:
            self.finished_signal.emit(self.video_url, self.index)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.log_signal.emit(f"Processing stopped for video {self.index} ({self.video_url})")

class DownloadWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(str, int, int)  # (playlist_url, processed, total)
    finished_signal = pyqtSignal(str)

    def __init__(self, playlist_url, save_path, volume_ratio, keep_original_audio, max_threads, video_quality):
        super().__init__()
        self.playlist_url = playlist_url
        # Извлекаем уникальную часть ссылки (например, list=PL0YH8fFyfiJLnIqljE44IyEXMfFBgHcOw)
        folder_name = self.extract_folder_name(playlist_url)
        self.save_path = os.path.join(save_path, folder_name)  # Путь с учётом имени папки
        os.makedirs(self.save_path, exist_ok=True)  # Создаём папку, если её нет
        self.volume_ratio = volume_ratio
        self.keep_original_audio = keep_original_audio
        self.max_threads = max_threads
        self.video_quality = video_quality
        self.processed_videos = set()
        self.total_videos = 0
        self.workers = []
        self.mutex = QMutex()
        self.completed_count = 0
        self.stop_requested = False
        self.task_queue = Queue()

    def extract_folder_name(self, url):
        """Извлекаем уникальную часть ссылки для имени папки."""
        match = re.search(r'list=([^&]+)', url)
        if match:
            return match.group(1)
        # Если не удалось извлечь list, используем последние 10 символов ссылки
        return url.replace(':', '_').replace('/', '_').replace('?', '_').replace('&', '_')[-10:]

    def run(self):
        try:
            self.log_signal.emit(f"Получение ссылок на видео: {self.playlist_url}")
            result = subprocess.run(["yt-dlp", "--flat-playlist", "--get-url", self.playlist_url], capture_output=True, text=True)
            video_urls = result.stdout.strip().split("\n")
            
            if not video_urls or result.returncode != 0:
                self.log_signal.emit(f"Ошибка получения ссылок для {self.playlist_url}: {result.stderr}")
                self.finished_signal.emit(self.playlist_url)
                return
            
            self.total_videos = len(video_urls)
            self.log_signal.emit(f"Найдено {self.total_videos} видео для {self.playlist_url}. Запуск обработки в папку {self.save_path}...")

            # Заполняем очередь задач
            for index, video_url in enumerate(video_urls, 1):
                self.task_queue.put((video_url, index))

            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                while (not self.task_queue.empty() or any(worker.isRunning() for worker in self.workers)) and not self.stop_requested:
                    # Запускаем новые задачи, если есть свободные слоты
                    while len([w for w in self.workers if w.isRunning()]) < self.max_threads and not self.task_queue.empty() and not self.stop_requested:
                        try:
                            video_url, index = self.task_queue.get_nowait()
                            worker = VideoProcessor(video_url, index, self.total_videos, self.save_path, self.volume_ratio, self.keep_original_audio, self.video_quality)
                            self.workers.append(worker)
                            worker.log_signal.connect(self.log_signal.emit)
                            worker.finished_signal.connect(self.on_video_processed)
                            executor.submit(worker.start)
                            self.log_signal.emit(f"Started worker for video {index}/{self.total_videos}")
                        except Queue.Empty:
                            break
                    
                    # Ждём, пока не освободится слот (или не будет запроса на остановку)
                    self.msleep(100)

        except Exception as e:
            self.log_signal.emit(f"Критическая ошибка: {str(e)}")
            self.finished_signal.emit(self.playlist_url)
        finally:
            if not self.stop_requested:
                self.check_completion()

    def on_video_processed(self, video_url, index):
        with QMutexLocker(self.mutex):
            if index not in self.processed_videos:
                self.processed_videos.add(index)
                processed_count = len(self.processed_videos)
                self.log_signal.emit(f"Progress update for {self.playlist_url}: Processed {processed_count}/{self.total_videos}")
                self.progress_signal.emit(self.playlist_url, processed_count, self.total_videos)

            self.completed_count += 1
            self.log_signal.emit(f"Completed {self.completed_count}/{self.total_videos} videos for {self.playlist_url}")

            # Удаляем завершённые потоки из списка workers
            self.workers = [worker for worker in self.workers if worker.isRunning()]

            if self.completed_count == self.total_videos:
                self.log_signal.emit(f"All videos processed for {self.playlist_url}!")
                self.finished_signal.emit(self.playlist_url)

    def stop(self):
        with QMutexLocker(self.mutex):
            self.stop_requested = True
            for worker in self.workers:
                if worker.isRunning():
                    worker.stop()
            self.log_signal.emit(f"Stopping processing for {self.playlist_url}...")

    def check_completion(self):
        with QMutexLocker(self.mutex):
            all_completed = len(self.processed_videos) == self.total_videos and not any(worker.isRunning() for worker in self.workers)
            if all_completed:
                self.log_signal.emit(f"All tasks completed for {self.playlist_url}!")
                self.finished_signal.emit(self.playlist_url)

class YouTubeDownloader(QWidget):
    def __init__(self):
        super().__init__()
        self.workers = {}
        self.available_qualities = ["best"]
        self.mutex = QMutex()
        self.quality_cache = {}
        self.current_quality_worker = None
        self.initUI()

    def initUI(self):
        self.setWindowTitle("YouTube Video Processor")
        self.setGeometry(100, 100, 600, 500)

        layout = QVBoxLayout()

        self.link_input = QLineEdit(self)
        self.link_input.setPlaceholderText("Введите ссылки на YouTube плейлисты через пробел")
        self.link_input.textChanged.connect(self.schedule_quality_update)
        layout.addWidget(self.link_input)

        self.loading_progress = QProgressBar(self)
        self.loading_progress.setRange(0, 0)
        self.loading_progress.setVisible(False)
        layout.addWidget(self.loading_progress)

        path_layout = QHBoxLayout()
        self.path_label = QLabel("Базовая папка для сохранения: Не выбрано", self)
        self.select_button = QPushButton("Выбрать папку", self)
        self.select_button.clicked.connect(self.select_folder)
        path_layout.addWidget(self.path_label)
        path_layout.addWidget(self.select_button)
        layout.addLayout(path_layout)

        self.volume_input = QSpinBox(self)
        self.volume_input.setRange(0, 100)
        self.volume_input.setValue(10)
        self.volume_input.setSuffix("%")
        layout.addWidget(QLabel("Громкость оригинальной дорожки (для метаданных):"))
        layout.addWidget(self.volume_input)
        
        self.keep_original_audio = QCheckBox("Сохранить обе аудиодорожки", self)
        self.keep_original_audio.setChecked(True)
        layout.addWidget(self.keep_original_audio)

        self.quality_label = QLabel("Качество видео:", self)
        layout.addWidget(self.quality_label)
        self.quality_combo = QComboBox(self)
        self.quality_combo.addItems(self.available_qualities)
        self.quality_combo.setCurrentText("best")
        layout.addWidget(self.quality_combo)

        self.threads_input = QSpinBox(self)
        self.threads_input.setRange(1, 16)
        self.threads_input.setValue(4)
        layout.addWidget(QLabel("Количество потоков для обработки:"))
        layout.addWidget(self.threads_input)
        
        self.playlist_progress_layout = QVBoxLayout()
        layout.addLayout(self.playlist_progress_layout)

        self.log_output = QTextEdit(self)
        self.log_output.setReadOnly(True)
        layout.addWidget(self.log_output)

        button_layout = QHBoxLayout()
        self.start_button = QPushButton("Запустить", self)
        self.start_button.clicked.connect(self.start_process)
        button_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Остановить", self)
        self.stop_button.clicked.connect(self.stop_process)
        self.stop_button.setEnabled(False)
        button_layout.addWidget(self.stop_button)

        layout.addLayout(button_layout)
        self.setLayout(layout)

    def schedule_quality_update(self):
        if self.current_quality_worker and self.current_quality_worker.isRunning():
            self.current_quality_worker.quit()
            self.current_quality_worker.wait()

        playlist_urls = self.link_input.text().strip().split()
        if playlist_urls:
            first_url = playlist_urls[0]
            if first_url and (first_url not in self.quality_cache or not self.quality_cache.get(first_url)):
                self.loading_progress.setVisible(True)
                self.current_quality_worker = QualityWorker(first_url)
                self.current_quality_worker.quality_signal.connect(self.update_quality_combo)
                self.current_quality_worker.finished_signal.connect(self.hide_loading_progress)
                self.current_quality_worker.log_signal.connect(self.log_output.append)
                self.current_quality_worker.start()

    def update_quality_combo(self, qualities):
        if qualities != self.available_qualities:
            self.available_qualities = qualities
            self.quality_combo.clear()
            self.quality_combo.addItems(self.available_qualities)
            self.quality_combo.setCurrentText(self.available_qualities[0])
            self.log_output.append(f"Updated quality options to: {self.available_qualities}")

    def hide_loading_progress(self):
        self.loading_progress.setVisible(False)

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите базовую папку для сохранения")
        if folder:
            self.save_path = folder
            self.path_label.setText(f"Базовая папка для сохранения: {folder}")

    def update_progress(self, playlist_url, processed, total):
        with QMutexLocker(self.mutex):
            worker = self.sender()
            if not worker:
                self.log_output.append(f"ERROR: No sender for progress update for {playlist_url}")
                return

            worker_index = list(self.workers.values()).index(worker)
            playlist_key = list(self.workers.keys())[worker_index]

            found = False
            for i in range(self.playlist_progress_layout.count()):
                item = self.playlist_progress_layout.itemAt(i)
                if item.widget() and isinstance(item.widget(), QWidget):
                    label = item.widget().findChild(QLabel)
                    progress = item.widget().findChild(QProgressBar)
                    if label and progress and label.text().startswith(playlist_key[:20] + "..."):
                        progress.setMaximum(total)
                        progress.setValue(processed)
                        label.setText(f"{playlist_key[:20]}... ({processed}/{total})")
                        found = True
                        self.log_output.append(f"DEBUG: Updated progress bar for {playlist_key}: {processed}/{total}")
                        break

            if not found:
                progress_widget = QWidget()
                progress_layout = QHBoxLayout()
                progress_label = QLabel(f"{playlist_key[:20]}... ({processed}/{total})")
                progress_bar = QProgressBar()
                progress_bar.setMaximum(total)
                progress_bar.setValue(processed)
                progress_layout.addWidget(progress_label)
                progress_layout.addWidget(progress_bar)
                progress_widget.setLayout(progress_layout)
                self.playlist_progress_layout.addWidget(progress_widget)
                self.log_output.append(f"DEBUG: Created new progress bar for {playlist_key}: {processed}/{total}")

    def start_process(self):
        playlist_urls = self.link_input.text().strip().split()
        if not playlist_urls:
            self.log_output.append("Ошибка: Введите ссылки на плейлисты!")
            return
        
        if not hasattr(self, 'save_path'):
            self.log_output.append("Ошибка: Выберите базовую папку для сохранения!")
            return
        
        volume_ratio = self.volume_input.value() / 100.0
        keep_original_audio = self.keep_original_audio.isChecked()
        max_threads = self.threads_input.value()
        video_quality = self.quality_combo.currentText()
        
        self.workers = {}

        for i in reversed(range(self.playlist_progress_layout.count())):
            self.playlist_progress_layout.itemAt(i).widget().setParent(None)

        for playlist_url in playlist_urls:
            worker = DownloadWorker(playlist_url, self.save_path, volume_ratio, keep_original_audio, max_threads, video_quality)
            self.workers[playlist_url] = worker
            worker.log_signal.connect(self.log_output.append)
            worker.progress_signal.connect(self.update_progress)
            worker.finished_signal.connect(self.check_completion)
            worker.start()

        self.log_output.append("Запуск параллельной обработки...")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def stop_process(self):
        for worker in self.workers.values():
            if worker:
                worker.stop()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        for i in reversed(range(self.playlist_progress_layout.count())):
            self.playlist_progress_layout.itemAt(i).widget().setParent(None)

    def check_completion(self, playlist_url):
        with QMutexLocker(self.mutex):
            all_completed = all(not worker.isRunning() for worker in self.workers.values() if worker is not None)
            if all_completed:
                self.log_output.append("Все задачи завершены!")
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YouTubeDownloader()
    window.show()
    sys.exit(app.exec())