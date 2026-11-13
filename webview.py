import sys
import os
import signal

os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
# Chromium GPU flags do not disable Qt's QQuickWidget/OpenGL compositor.
# Keep both rendering stages on the software path for the exhibition UI.
# Set before importing Qt or creating QApplication. Inference keeps using GPU.
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-gpu-compositing")
# 영상은 127.0.0.1 HTTP/MJPEG로만 받는다. 물리 NIC와 무관한 로컬 UI다.

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QColor, QKeySequence, QPalette
from PyQt5.QtWidgets import QApplication, QMainWindow, QShortcut
from PyQt5.QtWebEngineWidgets import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineView,
)
from PyQt5.QtWebEngineCore import QWebEngineUrlRequestInterceptor

app = QApplication(sys.argv)
app.setApplicationName("Jetson AI Modbus RTU")
app.setDesktopFileName("Jetson-AI-Modbus-RTU")
print(f'[UI] Qt Quick backend={os.environ["QT_QUICK_BACKEND"]}', flush=True)

# QtWebEngine's backing surface is white by default. On Jetson software
# compositing, a repaint gap can briefly expose that surface as a white flash.
dark_color = QColor("#111111")
dark_palette = app.palette()
dark_palette.setColor(QPalette.Window, dark_color)
dark_palette.setColor(QPalette.Base, dark_color)
app.setPalette(dark_palette)

UI_URL = QUrl("http://127.0.0.1:5022")

window = QMainWindow()
view = QWebEngineView()
window.setStyleSheet("background:#111111")
view.setStyleSheet("background:#111111")
# QWebEngine owns painting through its internal render widget. Do not suppress
# Qt background painting or promise opaque painting on its parent widgets:
# those attributes do not provide a retained Chromium frame buffer.

# jetsonctl.sh ui의 단일 실행 lock이 중복 창을 막는다. 기본 profile을 사용하면
# 종료할 때 page보다 profile이 먼저 파괴되는 Qt 경고와 불완전한 surface
# 해제를 피할 수 있으며 APACK/LwM2M과 같은 수명 주기를 사용한다.
profile = QWebEngineProfile.defaultProfile()
profile.setHttpCacheType(QWebEngineProfile.NoCache)
profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
page = QWebEnginePage(profile, view)
page.setBackgroundColor(dark_color)
view.setPage(page)

page.renderProcessTerminated.connect(
    lambda status, code: print(
        f'[UI] renderer terminated status={int(status)} exit={code}', flush=True))


def load_ui():
    view.load(UI_URL)


retry_timer = QTimer(window)
retry_timer.setSingleShot(True)
retry_timer.timeout.connect(load_ui)
load_watchdog = QTimer(window)
load_watchdog.setSingleShot(True)
load_watchdog.setInterval(15000)
_load_progress = -1
_stalled_load_retries = 0
_automatic_retry_enabled = True


def _on_load_started():
    global _load_progress
    retry_timer.stop()
    _load_progress = -1
    load_watchdog.start()
    print('[UI] page load started', flush=True)


def _on_load_progress(progress):
    global _load_progress
    if progress > _load_progress:
        _load_progress = progress
        load_watchdog.start()
    print(f'[UI] page load progress={progress}', flush=True)


def _on_load_finished(ok):
    global _stalled_load_retries
    load_watchdog.stop()
    print(f'[UI] page load finished ok={ok}', flush=True)
    if ok:
        _stalled_load_retries = 0
        retry_timer.stop()
        window.statusBar().hide()
    elif _automatic_retry_enabled and not retry_timer.isActive():
        retry_timer.start(1500)


def _on_load_stalled():
    global _stalled_load_retries, _automatic_retry_enabled
    _stalled_load_retries += 1
    print(f'[UI] page load stalled progress={_load_progress} '
          f'retry={_stalled_load_retries}', flush=True)
    if _stalled_load_retries <= 3:
        # Schedule before stop(): stop may emit loadFinished(False) immediately.
        # Both failure paths share one timer and cannot start concurrent loads.
        retry_timer.start(500)
    else:
        _automatic_retry_enabled = False
        retry_timer.stop()
        window.statusBar().show()
        window.statusBar().showMessage('화면 로드 실패 — F5로 다시 시도하세요. 로그를 확인해 주세요.')
    view.stop()


def reload_ui():
    global _stalled_load_retries, _automatic_retry_enabled
    _stalled_load_retries = 0
    _automatic_retry_enabled = True
    retry_timer.stop()
    view.reload()


page.loadStarted.connect(_on_load_started)
page.loadProgress.connect(_on_load_progress)
page.loadFinished.connect(_on_load_finished)
load_watchdog.timeout.connect(_on_load_stalled)
# QNetworkAccessManager loads Qt's NetworkManager bearer plugin even for a
# localhost-only URL.  That plugin prints stale D-Bus object warnings whenever
# Wi-Fi is toggled.  QWebEngine's failed-load retry is sufficient here and does
# not couple the local UI to NetworkManager state.
window.setCentralWidget(view)
window.setWindowTitle("Jetson Flask UI")
window.resize(1280, 720)

# 바탕화면 아이콘을 다시 누르면 jetsonctl.sh가 SIGUSR1을 보낸다. Qt API를
# signal handler에서 직접 호출하지 않고 타이머가 GUI thread에서 창을 복원한다.
_raise_requested = False


def _request_window_raise(_signum, _frame):
    global _raise_requested
    _raise_requested = True


def _raise_window_if_requested():
    global _raise_requested
    if not _raise_requested:
        return
    _raise_requested = False
    if window.isMinimized():
        window.setWindowState(
            (window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
        )
    window.show()
    window.raise_()
    window.activateWindow()


signal.signal(signal.SIGUSR1, _request_window_raise)
raise_timer = QTimer(window)
raise_timer.timeout.connect(_raise_window_if_requested)
raise_timer.start(100)

# QWebEngineView does not always forward browser reload shortcuts itself.
# Bind them at the window level so a field deployment can reload new UI code.
reload_ctrl_r = QShortcut(QKeySequence("Ctrl+R"), window)
reload_ctrl_r.activated.connect(reload_ui)
reload_f5 = QShortcut(QKeySequence("F5"), window)
reload_f5.activated.connect(reload_ui)

def _show_and_raise_window():
    window.show()
    window.raise_()
    window.activateWindow()


# The first desktop launch can race the compositor startup notification.
# Raise again after the native window has been mapped, so a second click is
# never required just to make the already-created window visible.
_show_and_raise_window()
QTimer.singleShot(300, _show_and_raise_window)
QTimer.singleShot(1000, _show_and_raise_window)
# Construct/attach/show the native render widget first, then navigate from the
# GUI event loop. Starting navigation before setCentralWidget/show exposed an
# intermittent startup hang (loadStarted without loadFinished).
QTimer.singleShot(0, load_ui)

sys.exit(app.exec_())
