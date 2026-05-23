import sys
import socket
import time
import struct
import json
import os
import shutil
import ctypes
from pathlib import Path
import cv2
import numpy as np
from mss import mss

from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QComboBox, QSlider, QFrame, QGraphicsDropShadowEffect)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap, QColor, QIcon

# ===========================
# 1. 核心推流工作线程 (已加入画面缩放与 XOR 加密)
# ===========================
class StreamWorker(QThread):
    frame_captured = pyqtSignal(QImage)
    fps_updated = pyqtSignal(int)
    status_updated = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.is_running = False
        self.sock = None
        self.ip = "127.0.0.1"
        self.port = 7878
        self.width = 256
        self.height = 256
        self.quality = 80
        self.fps_limit = 120
        self.protocol = "TCP"

    def request_stop(self):
        self.is_running = False
        sock = self.sock
        if not sock:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def run(self):
        self.is_running = True
        sock = None
        addr = (self.ip, self.port)
        
        try:
            if self.protocol == "TCP":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                self.status_updated.emit(f"正在连接 TCP -> {self.ip}:{self.port}...")
                sock.connect(addr)
                sock.settimeout(None)
                self.status_updated.emit(f"TCP 推流中-> {self.ip}")
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.status_updated.emit(f"UDP 发送中 -> {self.ip}:{self.port}")
            self.sock = sock

            with mss() as sct:
                monitor = sct.monitors[1]
                screen_w = monitor['width']
                screen_h = monitor['height']
                capture_w = min(self.width, screen_w)
                capture_h = min(self.height, screen_h)

                if capture_w <= 0 or capture_h <= 0:
                    raise ValueError("推流分辨率必须大于 0")
                if capture_w != self.width or capture_h != self.height:
                    self.status_updated.emit(f"分辨率超出屏幕，已自动裁切为 {capture_w}x{capture_h}")
                
                fps_counter = 0
                last_fps_time = time.time()
                last_udp_warn_time = 0.0

                while self.is_running:
                    loop_start = time.time()

                    # 计算中心裁切
                    left = (screen_w - capture_w) // 2
                    top = (screen_h - capture_h) // 2
                    region = {"top": top, "left": left, "width": capture_w, "height": capture_h}

                    img_bgra = np.array(sct.grab(region))
                    frame_bgr = cv2.cvtColor(img_bgra, cv2.COLOR_BGRA2BGR)

                    # ====== 优化 1：游戏画面等比例降采样 (防止 UDP 单包超过 60KB 导致画面撕裂丢帧) ======
                    # 默认缩小到 0.6 倍，你可以根据你的内网带宽质量调整该系数（如 0.5 到 0.8）
                    frame_bgr = cv2.resize(frame_bgr, (0, 0), fx=0.6, fy=0.6, interpolation=cv2.INTER_LINEAR)
                    # ============================================================================

                    # UI预览 (BGR -> RGB)
                    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                    h, w, ch = frame_rgb.shape
                    qt_img = QImage(frame_rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
                    self.frame_captured.emit(qt_img)

                    # 编码
                    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
                    _, encimg = cv2.imencode('.jpg', frame_bgr, encode_param)

                    # ====== 优化 2：内存连续性安全转换与位异或（XOR）加密 ======
                    raw_bytes = encimg.tobytes()
                    XOR_KEY = 153  # 流量加密密钥 (0-255)，接收端解密时必须也填写 153
                    data_np = np.frombuffer(raw_bytes, dtype=np.uint8)
                    encrypted_np = np.bitwise_xor(data_np, XOR_KEY)
                    data = encrypted_np.tobytes()
                    # =========================================================

                    try:
                        if self.protocol == "TCP":
                            header = struct.pack(">L", len(data))
                            sock.sendall(header + data)
                        else:
                            if len(data) < 60000:
                                sock.sendto(data, addr)
                            else:
                                now = time.time()
                                if now - last_udp_warn_time >= 1.0:
                                    self.status_updated.emit(f"UDP 包过大({len(data)} bytes)，该帧已丢弃，请降低画质/分辨率")
                                last_udp_warn_time = now
                    except Exception as e:
                        if self.is_running:
                            self.status_updated.emit(f"发送失败: {str(e)}")
                        break

                    # FPS 统计与限制
                    fps_counter += 1
                    if time.time() - last_fps_time >= 1.0:
                        self.fps_updated.emit(fps_counter)
                        fps_counter = 0
                        last_fps_time = time.time()

                    elapsed = time.time() - loop_start
                    wait_time = (1.0 / self.fps_limit) - elapsed
                    if wait_time > 0:
                        time.sleep(wait_time)

        except Exception as e:
            self.status_updated.emit(f"错误: {str(e)}")
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            self.sock = None
            self.is_running = False
            self.status_updated.emit("Stopped")


def get_logo_icon():
    icon_path = Path(__file__).resolve().parent / "logo.ico"
    if icon_path.exists():
        return QIcon(str(icon_path))
    return QIcon()


def set_windows_app_user_model_id(app_id):
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass

# ===========================
# 2. 支持换肤的自定义控件
# ===========================
class ModernInput(QLineEdit):
    def update_theme(self, is_dark):
        if is_dark:
            self.setStyleSheet("""
                QLineEdit { background-color: #3A3A3C; border: 1px solid #48484A; border-radius: 8px; color: white; padding: 5px 10px; font-size: 13px; }
                QLineEdit:focus { border: 1px solid #0A84FF; background-color: #48484A; }
            """)
        else:
            self.setStyleSheet("""
                QLineEdit { background-color: #FFFFFF; border: 1px solid #D1D1D6; border-radius: 8px; color: black; padding: 5px 10px; font-size: 13px; }
                QLineEdit:focus { border: 1px solid #007AFF; background-color: #F2F2F7; }
            """)

class ModernButton(QPushButton):
    def __init__(self, text, is_primary=False, parent=None):
        super().__init__(text, parent)
        self.is_primary = is_primary

    def update_theme(self, is_dark):
        if self.is_primary:
            bg, hover, text = ("#0A84FF", "#409CFF", "white")
        else:
            bg = "#3A3A3C" if is_dark else "#E5E5EA"
            hover = "#48484A" if is_dark else "#D1D1D6"
            text = "white" if is_dark else "black"

        self.setStyleSheet(f"""
            QPushButton {{ background-color: {bg}; color: {text}; border-radius: 8px; padding: 8px 15px; font-weight: bold; font-size: 13px; border: none; }}
            QPushButton:hover {{ background-color: {hover}; }}
        """)

class ThemeToggleButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("☀", parent)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    
    def update_theme(self, is_dark):
        if is_dark:
            self.setStyleSheet("""
                QPushButton { background-color: rgba(255,255,255,0.1); color: #FFD60A; border-radius: 15px; font-size: 18px; border: 1px solid #48484A; }
                QPushButton:hover { background-color: rgba(255,255,255,0.2); }
            """)
        else:
            self.setStyleSheet("""
                QPushButton { background-color: #FFFFFF; color: #FF9500; border-radius: 15px; font-size: 18px; border: 1px solid #D1D1D6; }
                QPushButton:hover { background-color: #F2F2F7; }
            """)

# ===========================
# 3. 主窗口
# ===========================
class ModernStreamerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(720, 460)
        icon = get_logo_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        
        self.app_name = "HX Streamer Pro"
        self.is_dark_mode = True
        self.config_path = self.resolve_config_path()
        self.legacy_config_path = Path(__file__).resolve().parent / "config.json"
        self.ensure_config_directory()
        self.migrate_legacy_config()
        
        self.is_loading_config = False
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.setSingleShot(True)
        self.auto_save_timer.setInterval(500)
        self.auto_save_timer.timeout.connect(self.save_config)
        self.worker = StreamWorker()
        self.worker.frame_captured.connect(self.update_preview)
        self.worker.fps_updated.connect(self.update_fps)
        self.worker.status_updated.connect(self.update_status)
        self.worker.finished.connect(self.on_worker_finished)
        
        self.init_ui()
        self.bind_auto_save_events()
        self.load_config()
        self.apply_theme()
        self.center_window()
        self.old_pos = None

    def init_ui(self):
        self.main_widget = QFrame()
        self.main_widget.setObjectName("MainFrame")
        
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 80))
        shadow.setOffset(0, 5)
        self.main_widget.setGraphicsEffect(shadow)
        self.setCentralWidget(self.main_widget)
        
        main_layout = QHBoxLayout(self.main_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        # === 左侧控制栏 ===
        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(10)
        
        header_layout = QHBoxLayout()
        self.title_lbl = QLabel(self.app_name)
        self.title_lbl.setStyleSheet("font-size: 16px; font-weight: bold;")
        
        self.btn_theme = ThemeToggleButton()
        self.btn_theme.clicked.connect(self.toggle_theme)
        
        self.btn_close = QPushButton("×")
        self.btn_close.setFixedSize(24, 24)
        self.btn_close.clicked.connect(self.close)
        
        header_layout.addWidget(self.title_lbl)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_theme)
        header_layout.addSpacing(8)
        header_layout.addWidget(self.btn_close)
        controls_layout.addLayout(header_layout)
        
        controls_layout.addSpacing(5)

        self.status_lbl = QLabel("Status: Ready")
        self.status_lbl.setStyleSheet("font-size: 12px; color: #888;")
        controls_layout.addWidget(self.status_lbl)

        self.fps_lbl = QLabel("FPS: 0")
        self.fps_lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #30D158;")
        controls_layout.addWidget(self.fps_lbl)
        
        form_layout = QVBoxLayout()
        form_layout.setSpacing(8)
        
        self.lbl_proto = QLabel("Protocol:")
        form_layout.addWidget(self.lbl_proto)
        self.proto_combo = QComboBox()
        self.proto_combo.addItems(["TCP", "UDP (Fast)"])
        self.proto_combo.setFixedHeight(30)
        form_layout.addWidget(self.proto_combo)

        form_layout.addWidget(QLabel("Target IP:"))
        self.inp_ip = ModernInput("192.168.1.1")
        form_layout.addWidget(self.inp_ip)
        
        form_layout.addWidget(QLabel("Port:"))
        self.inp_port = ModernInput("7878")
        form_layout.addWidget(self.inp_port)

        size_box = QHBoxLayout()
        self.inp_w = ModernInput("256")
        self.inp_h = ModernInput("256")
        size_box.addWidget(QLabel("W:"))
        size_box.addWidget(self.inp_w)
        size_box.addWidget(QLabel("H:"))
        size_box.addWidget(self.inp_h)
        form_layout.addLayout(size_box)
        controls_layout.addLayout(form_layout)

        controls_layout.addSpacing(10)

        self.lbl_quality_title = QLabel("Quality: 80")
        controls_layout.addWidget(self.lbl_quality_title)
        
        self.slider_quality = QSlider(Qt.Orientation.Horizontal)
        self.slider_quality.setRange(10, 100)
        self.slider_quality.setValue(80)
        self.slider_quality.valueChanged.connect(self.on_quality_change)
        controls_layout.addWidget(self.slider_quality)

        self.lbl_fps_title = QLabel("FPS Limit: 120")
        controls_layout.addWidget(self.lbl_fps_title)
        
        self.slider_fps = QSlider(Qt.Orientation.Horizontal)
        self.slider_fps.setRange(1, 500)
        self.slider_fps.setValue(120)
        self.slider_fps.valueChanged.connect(self.on_fps_change)
        controls_layout.addWidget(self.slider_fps)

        controls_layout.addStretch()

        self.btn_action = ModernButton("Start Streaming", is_primary=True)
        self.btn_action.setFixedHeight(40)
        self.btn_action.clicked.connect(self.toggle_stream)
        controls_layout.addWidget(self.btn_action)

        # === 右侧预览区 ===
        self.preview_container = QFrame()
        self.preview_container.setObjectName("PreviewFrame")
        preview_layout = QVBoxLayout(self.preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        
        self.lbl_preview = QLabel("Preview Paused")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.lbl_preview)

        main_layout.addLayout(controls_layout, 3)
        main_layout.addWidget(self.preview_container, 5)
        
        self.theme_widgets = [self.inp_ip, self.inp_port, self.inp_w, self.inp_h, self.btn_action]

    def bind_auto_save_events(self):
        for widget in [self.inp_ip, self.inp_port, self.inp_w, self.inp_h]:
            widget.textChanged.connect(self.schedule_auto_save)
        self.proto_combo.currentIndexChanged.connect(self.schedule_auto_save)

    def resolve_config_path(self):
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / self.app_name / "config.json"
        return Path.home() / ".hx_streamer_pro" / "config.json"

    def ensure_config_directory(self):
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"Create config dir failed: {e}")

    def migrate_legacy_config(self):
        if self.config_path.exists() or not self.legacy_config_path.exists():
            return
        try:
            shutil.copy2(self.legacy_config_path, self.config_path)
        except Exception as e:
            print(f"Migrate legacy config failed: {e}")

    def parse_int(self, value, default, min_value=None, max_value=None):
        try:
            result = int(value)
        except (TypeError, ValueError):
            return default
        if min_value is not None and result < min_value:
            return default
        if max_value is not None and result > max_value:
            return default
        return result

    def get_config_data(self):
        return {
            "ip": self.inp_ip.text().strip(),
            "port": self.inp_port.text().strip(),
            "width": self.inp_w.text().strip(),
            "height": self.inp_h.text().strip(),
            "quality": self.slider_quality.value(),
            "fps_limit": self.slider_fps.value(),
            "protocol_index": self.proto_combo.currentIndex(),
            "is_dark_mode": self.is_dark_mode
        }

    def load_config(self):
        if not self.config_path.exists():
            return

        try:
            with self.config_path.open("r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Load config failed: {e}")
            return

        self.is_loading_config = True
        self.inp_ip.setText(str(config.get("ip", self.inp_ip.text())))
        self.inp_port.setText(str(config.get("port", self.inp_port.text())))
        self.inp_w.setText(str(config.get("width", self.inp_w.text())))
        self.inp_h.setText(str(config.get("height", self.inp_h.text())))

        quality = self.parse_int(config.get("quality"), self.slider_quality.value(), 10, 100)
        fps_limit = self.parse_int(config.get("fps_limit"), self.slider_fps.value(), 1, 500)
        self.slider_quality.setValue(quality)
        self.slider_fps.setValue(fps_limit)

        protocol_index = config.get("protocol_index")
        if protocol_index is None:
            protocol = str(config.get("protocol", "TCP")).upper()
            protocol_index = 1 if protocol.startswith("UDP") else 0
        protocol_index = self.parse_int(protocol_index, 0, 0, 1)
        self.proto_combo.setCurrentIndex(protocol_index)

        if isinstance(config.get("is_dark_mode"), bool):
            self.is_dark_mode = config["is_dark_mode"]

        self.lbl_quality_title.setText(f"Quality: {self.slider_quality.value()}")
        self.lbl_fps_title.setText(f"FPS Limit: {self.slider_fps.value()}")
        self.is_loading_config = False

    def save_config(self):
        if self.is_loading_config:
            return
        try:
            self.ensure_config_directory()
            with self.config_path.open("w", encoding="utf-8") as f:
                json.dump(self.get_config_data(), f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Save config failed: {e}")

    def schedule_auto_save(self, *_):
        if self.is_loading_config:
            return
        self.auto_save_timer.start()

    def on_quality_change(self, value):
        self.lbl_quality_title.setText(f"Quality: {value}")
        if self.worker.isRunning():
            self.worker.quality = value
        self.schedule_auto_save()

    def on_fps_change(self, value):
        self.lbl_fps_title.setText(f"FPS Limit: {value}")
        if self.worker.isRunning():
            self.worker.fps_limit = value
        self.schedule_auto_save()

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.apply_theme()
        self.schedule_auto_save()

    def apply_theme(self):
        if self.is_dark_mode:
            bg_main = "#1C1C1E"
            bg_preview = "#000000"
            text_color = "#E5E5E5"
            border_color = "#333333"
            close_bg = "#FF453A"
            combo_bg = "#3A3A3C"
            combo_border = "#48484A"
        else:
            bg_main = "#F2F2F7"
            bg_preview = "#E5E5EA"
            text_color = "#1C1C1E"
            border_color = "#D1D1D6"
            close_bg = "#FF3B30"
            combo_bg = "#FFFFFF"
            combo_border = "#D1D1D6"

        self.main_widget.setStyleSheet(f"""
            #MainFrame {{
                background-color: {bg_main};
                border-radius: 16px;
                border: 1px solid {border_color};
            }}
            QLabel {{ color: {text_color}; font-family: 'Segoe UI', sans-serif; }}
        """)
        
        self.preview_container.setStyleSheet(f"""
            #PreviewFrame {{
                background-color: {bg_preview};
                border-radius: 12px;
                border: 1px solid {border_color};
            }}
        """)

        self.btn_close.setStyleSheet(f"""
            QPushButton {{ background-color: {close_bg}; border-radius: 12px; color: white; font-weight: bold; }}
            QPushButton:hover {{ background-color: red; }}
        """)

        self.proto_combo.setStyleSheet(f"""
            QComboBox {{ background-color: {combo_bg}; color: {text_color}; border-radius: 8px; padding: 5px; border: 1px solid {combo_border}; }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{ background-color: {combo_bg}; color: {text_color}; selection-background-color: #0A84FF; }}
        """)

        self.title_lbl.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {text_color};")
        
        for widget in self.theme_widgets:
            if hasattr(widget, 'update_theme'):
                widget.update_theme(self.is_dark_mode)
        
        self.btn_theme.update_theme(self.is_dark_mode)
        if self.worker.isRunning():
            self.apply_stop_button_style()

    def set_stream_inputs_enabled(self, enabled):
        for widget in [self.inp_ip, self.inp_port, self.inp_w, self.inp_h, self.proto_combo]:
            widget.setEnabled(enabled)

    def apply_stop_button_style(self):
        self.btn_action.setStyleSheet("""
            QPushButton { background-color: #FF453A; color: white; border-radius: 8px; border: none; font-weight: bold; font-size: 13px; }
            QPushButton:hover { background-color: #FF5D55; }
        """)

    def collect_stream_settings(self):
        ip = self.inp_ip.text().strip()
        if not ip:
            self.status_lbl.setText("Error: IP 不能为空")
            return None

        port = self.parse_int(self.inp_port.text(), None, 1, 65535)
        if port is None:
            self.status_lbl.setText("Error: 端口范围应为 1-65535")
            return None

        screen = QApplication.primaryScreen()
        max_w, max_h = 8192, 8192
        if screen:
            size = screen.size()
            max_w, max_h = size.width(), size.height()

        width = self.parse_int(self.inp_w.text(), None, 16, max_w)
        if width is None:
            self.status_lbl.setText(f"Error: 宽度范围应为 16-{max_w}")
            return None

        height = self.parse_int(self.inp_h.text(), None, 16, max_h)
        if height is None:
            self.status_lbl.setText(f"Error: 高度范围应为 16-{max_h}")
            return None

        return {
            "ip": ip,
            "port": port,
            "width": width,
            "height": height,
            "quality": self.slider_quality.value(),
            "fps_limit": self.slider_fps.value(),
            "protocol": "TCP" if self.proto_combo.currentIndex() == 0 else "UDP"
        }

    def toggle_stream(self):
        if not self.worker.isRunning():
            settings = self.collect_stream_settings()
            if not settings:
                return

            self.worker.ip = settings["ip"]
            self.worker.port = settings["port"]
            self.worker.width = settings["width"]
            self.worker.height = settings["height"]
            self.worker.quality = settings["quality"]
            self.worker.fps_limit = settings["fps_limit"]
            self.worker.protocol = settings["protocol"]
            self.save_config()

            self.worker.start()
            self.btn_action.setText("Stop Streaming")
            self.apply_stop_button_style()
            self.set_stream_inputs_enabled(False)
        else:
            self.status_lbl.setText("Stopping...")
            self.worker.request_stop()
            self.btn_action.setEnabled(False)
            self.btn_action.setText("Stopping...")

    def on_worker_finished(self):
        self.btn_action.setEnabled(True)
        self.btn_action.setText("Start Streaming")
        self.btn_action.update_theme(self.is_dark_mode)
        self.set_stream_inputs_enabled(True)
        self.lbl_preview.setText("Preview Paused")
        self.lbl_preview.setPixmap(QPixmap())

    def update_preview(self, qt_img):
        pixmap = QPixmap.fromImage(qt_img)
        scaled_pixmap = pixmap.scaled(self.lbl_preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.lbl_preview.setPixmap(scaled_pixmap)

    def update_fps(self, fps):
        self.fps_lbl.setText(f"FPS: {fps}")

    def update_status(self, text):
        self.status_lbl.setText(text)

    def center_window(self):
        qr = self.frameGeometry()
        cp = self.screen().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.old_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.old_pos:
            delta = event.globalPosition().toPoint() - self.old_pos
            self.move(self.pos() + delta)
            self.old_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.old_pos = None

    def closeEvent(self, event):
        self.auto_save_timer.stop()
        self.save_config()
        if self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(1500)
        super().closeEvent(event)

if __name__ == "__main__":
    set_windows_app_user_model_id("AouTzxc.HXStreamerPro")
    app = QApplication(sys.argv)
    app_icon = get_logo_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    window = ModernStreamerApp()
    window.show()
    sys.exit(app.exec())