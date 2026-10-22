"""Embedded desktop frontend. Qt imports stay lazy for headless server/tests."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

import launch
from runtime_paths import data_root


def same_origin(url, origin):
    try:
        target, source = urlsplit(url), urlsplit(origin)
        return (target.scheme == source.scheme == 'http' and
                target.hostname == source.hostname == '127.0.0.1' and
                target.port == source.port and not target.username and not target.password)
    except (TypeError, ValueError):
        return False


def navigation_allowed(url, origin):
    if not isinstance(url, str):
        return False
    if url == 'about:blank' or url.startswith(('data:', 'qrc:')):
        return True
    if url.startswith('blob:'):
        return same_origin(url[5:], origin)
    return bool(origin) and same_origin(url, origin)


def download_name(value):
    value = str(value or '').replace('\\', '/').rsplit('/', 1)[-1]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', value).strip(' .')[:180].strip(' .')
    if not value or value.split('.', 1)[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
            *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        return '追因结果.json'
    return value


def instance_name():
    from runtime_paths import desktop_identity
    identity = desktop_identity() or {'data_id': str(data_root().resolve()).casefold()}
    return 'zhuiyin-'+hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def run_desktop(page='workbench'):
    from PySide6.QtCore import QObject, Signal, QTimer, QUrl, QStandardPaths, Qt
    from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import (QApplication, QMainWindow, QStackedWidget, QWidget,
        QVBoxLayout, QLabel, QPushButton, QFileDialog, QMessageBox)
    from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
        QWebEngineDownloadRequest, QWebEngineUrlRequestInterceptor)
    from PySide6.QtWebEngineWidgets import QWebEngineView

    app = QApplication.instance() or QApplication(['追因'])
    app.setApplicationName('追因')
    app.setOrganizationName('Zhuiyin')
    app.setStyle('Fusion')
    icon_image = QPixmap(64, 64)
    icon_image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(icon_image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor('#008f82'))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(1, 1, 62, 62, 15, 15)
    painter.setPen(QPen(QColor('white'), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(19, 45, 45, 19)
    painter.drawLine(32, 19, 45, 19)
    painter.drawLine(45, 19, 45, 32)
    painter.end()
    app.setWindowIcon(QIcon(icon_image))

    # Repeated launches activate the existing window and preserve its form/key.
    socket = QLocalSocket()
    name = instance_name()
    socket.connectToServer(name)
    if socket.waitForConnected(200):
        socket.write(b'activate\n')
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return 0
    ipc = QLocalServer(app)
    ipc.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not ipc.listen(name):
        raise RuntimeError('无法创建软件窗口的本机会话，请稍后重试。')

    class Signals(QObject):
        status = Signal(str)
        ready = Signal(str)
        error = Signal(str)
        stopped = Signal(bool, str)

    class Filter(QWebEngineUrlRequestInterceptor):
        def __init__(self, parent):
            super().__init__(parent)
            self.origin = ''

        def interceptRequest(self, info):
            if not navigation_allowed(info.requestUrl().toString(), self.origin):
                info.block(True)

    class LocalPage(QWebEnginePage):
        def __init__(self, profile, view, owner):
            super().__init__(profile, view)
            self.owner = owner

        def acceptNavigationRequest(self, url, kind, main_frame):
            allowed = navigation_allowed(url.toString(), self.owner.origin)
            if main_frame and url.scheme() in ('data', 'qrc'):
                allowed = False
            if not allowed and main_frame:
                self.owner.statusBar().showMessage('本窗口只打开追因本机页面。', 6000)
            return allowed

        def javaScriptConsoleMessage(self, level, message, line, source):
            # Do not put arbitrary form/provider text into durable logs.
            pass

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.origin = ''
            self.home = None
            self.starting = True
            self.closing = False
            self.allow_close = False
            self.active_downloads = set()
            self.setWindowTitle('追因')
            self.resize(1280, 850)
            self.setMinimumSize(900, 650)
            self.setWindowIcon(app.windowIcon())
            self.stack = QStackedWidget()
            self.setCentralWidget(self.stack)
            self.loading = QWidget()
            layout = QVBoxLayout(self.loading)
            layout.setContentsMargins(60, 50, 60, 50)
            layout.addStretch()
            title = QLabel('追因')
            title.setStyleSheet('font-size:36px;font-weight:bold;color:#008f82')
            layout.addWidget(title)
            self.message = QLabel('正在准备工作台…')
            self.message.setWordWrap(True)
            self.message.setStyleSheet('font-size:17px;padding-top:14px')
            layout.addWidget(self.message)
            self.retry = QPushButton('重试启动')
            self.retry.hide()
            self.retry.clicked.connect(self.start_service)
            layout.addWidget(self.retry)
            layout.addStretch()
            self.stack.addWidget(self.loading)
            self.view = QWebEngineView()
            # An unnamed profile keeps form values, API keys, cookies and cache
            # in memory for this process; no browser account/profile is imported.
            self.profile = QWebEngineProfile(self.view)
            self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            self.filter = Filter(self.profile)
            self.profile.setUrlRequestInterceptor(self.filter)
            self.webpage = LocalPage(self.profile, self.view, self)
            self.view.setPage(self.webpage)
            self.webpage.newWindowRequested.connect(lambda request: self.view.setUrl(request.requestedUrl())
                if navigation_allowed(request.requestedUrl().toString(), self.origin) else None)
            self.profile.downloadRequested.connect(self.save_download)
            self.view.loadFinished.connect(self.loaded)
            self.stack.addWidget(self.view)
            menu = self.menuBar().addMenu('视图')
            for text, shortcut, callback in (
                ('放大', 'Ctrl++', lambda: self.zoom(0.1)),
                ('缩小', 'Ctrl+-', lambda: self.zoom(-0.1)),
                ('恢复100%大小', 'Ctrl+0', lambda: self.view.setZoomFactor(1.0))):
                action = QAction(text, self)
                action.setShortcut(shortcut)
                action.triggered.connect(callback)
                menu.addAction(action)
            self.signals = Signals(self)
            self.signals.status.connect(self.message.setText)
            self.signals.ready.connect(self.service_ready)
            self.signals.error.connect(self.failed)
            self.signals.stopped.connect(self.stopped)
            QTimer.singleShot(0, self.start_service)

        def start_service(self):
            self.starting = True
            self.retry.hide()
            self.message.setText('正在启动本机实验服务…')
            def work():
                try:
                    self.signals.ready.emit(launch.ensure_service(page, self.signals.status.emit))
                except Exception:
                    self.signals.error.emit('本机服务未能启动。请查看“追因数据”中的启动日志后重试。')
            threading.Thread(target=work, daemon=True).start()

        def service_ready(self, url):
            self.starting = False
            self.home = url
            parts = urlsplit(url)
            self.origin = f'{parts.scheme}://{parts.netloc}'
            self.filter.origin = self.origin
            if self.closing:
                self.request_stop()
                return
            self.statusBar().showMessage('正在载入工作台…')
            self.view.setUrl(QUrl(url))

        def failed(self, message):
            self.starting = False
            if self.closing:
                self.allow_close = True
                self.close()
                return
            self.message.setText(message)
            self.retry.show()
            self.statusBar().showMessage('启动未完成')

        def loaded(self, ok):
            if ok:
                self.stack.setCurrentWidget(self.view)
                self.statusBar().showMessage('本机服务已就绪 · 所有操作在当前窗口进行')
                if os.environ.get('ZHUIYIN_EMBEDDED_PROBE') == '1':
                    QTimer.singleShot(1000, self.probe)
            else:
                self.stack.setCurrentWidget(self.loading)
                self.message.setText('工作台暂时没有载入，请点击重试；程序不会唤起外部浏览器。')
                self.retry.show()

        def zoom(self, delta):
            self.view.setZoomFactor(max(0.7, min(2.0, self.view.zoomFactor()+delta)))

        def save_download(self, request):
            directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
            proposed = str(Path(directory or str(data_root()))/download_name(request.downloadFileName()))
            if os.environ.get('ZHUIYIN_EMBEDDED_PROBE') == '1':
                target = str(data_root()/'embedded-probe-download.csv')
            else:
                target, _ = QFileDialog.getSaveFileName(self, '保存追因结果', proposed)
            if not target:
                request.cancel()
                return
            path = Path(target)
            request.setDownloadDirectory(str(path.parent))
            request.setDownloadFileName(path.name)
            key = id(request)
            self.active_downloads.add(key)
            def state_changed(state):
                if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                    self.active_downloads.discard(key)
                    self.statusBar().showMessage('已保存：'+str(path), 15000)
                    if os.environ.get('ZHUIYIN_EMBEDDED_PROBE') == '1':
                        self.probe_download_complete(path)
                elif state in (QWebEngineDownloadRequest.DownloadState.DownloadCancelled,
                               QWebEngineDownloadRequest.DownloadState.DownloadInterrupted):
                    self.active_downloads.discard(key)
                    self.statusBar().showMessage('下载未完成，请检查保存位置后重试。', 10000)
            request.stateChanged.connect(state_changed)
            request.accept()

        def request_stop(self):
            self.closing = True
            self.statusBar().showMessage('正在保存并退出…')
            def work():
                try:
                    from desktop_entry import stop_service
                    stop_service()
                    self.signals.stopped.emit(True, '')
                except Exception:
                    self.signals.stopped.emit(False, '实验可能仍在运行。请先在页面停止或等待完成，再关闭软件。')
            threading.Thread(target=work, daemon=True).start()

        def stopped(self, ok, message):
            if ok:
                self.allow_close = True
                self.close()
            else:
                self.closing = False
                self.statusBar().showMessage(message)
                QMessageBox.information(self, '暂未退出追因', message)

        def closeEvent(self, event):
            if self.allow_close:
                event.accept()
            elif self.active_downloads:
                event.ignore()
                QMessageBox.information(self, '正在保存结果', '请等待当前下载完成后再关闭软件。')
            else:
                event.ignore()
                if not self.closing:
                    self.closing = True
                    if self.starting:
                        self.statusBar().showMessage('正在结束启动，请稍候…')
                    elif self.home:
                        self.request_stop()
                    else:
                        self.allow_close = True
                        self.close()

        def probe(self):
            # Read-only DOM checks of this test instance; no model calls.
            self.webpage.runJavaScript('JSON.stringify({title:document.title,ready:document.querySelector("#health-label")?.textContent,'
                'nav:[...document.querySelectorAll(".top-nav a,nav a")].map(a=>a.textContent),'
                'fileInputs:document.querySelectorAll("input[type=file]").length,'
                'batchMultiple:document.querySelector("#test-files")?.multiple,'
                'workbench:!!document.querySelector("#experiment-form")})', self.probe_dom)

        def probe_dom(self, value):
            try:
                dom = json.loads(value)
            except (ValueError, TypeError):
                dom = {'invalid': True}
            self.probe_result = {'window_visible': self.isVisible(), 'dom': dom,
                'engine': 'QtWebEngine', 'off_the_record': self.profile.isOffTheRecord(),
                'url': self.view.url().toString(), 'external_browser_requested': False}
            self.webpage.download(QUrl(self.origin+'/api/templates/scenarios.csv'), 'scenarios.csv')

        def probe_download_complete(self, path):
            self.probe_result['download_bytes'] = path.stat().st_size
            self.probe_result['download_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.grab().save(str(data_root()/'embedded-window.png'))
            (data_root()/'embedded-check.json').write_text(json.dumps(self.probe_result, ensure_ascii=False, indent=2), encoding='utf-8')
            QTimer.singleShot(300, self.close)

    window = Window()
    def activate():
        while ipc.hasPendingConnections():
            client = ipc.nextPendingConnection()
            client.disconnectFromServer()
            client.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    ipc.newConnection.connect(activate)
    window.show()
    code = app.exec()
    ipc.close()
    return code
