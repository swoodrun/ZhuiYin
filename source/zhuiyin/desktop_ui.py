"""Visible desktop status; the application itself keeps its existing web UI."""
import html
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

import launch
from runtime_paths import data_root


class LauncherWindow:
    def __init__(self, page='workbench'):
        self.page = page
        self.url = None
        self.messages = queue.Queue()
        self.root = tk.Tk()
        self.root.title('追因 · 启动与运行状态')
        self.root.geometry('660x370')
        self.root.minsize(600, 370)
        self.root.configure(bg='#f5faf9')
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('TButton', padding=(12, 9))
        style.configure('Primary.TButton', foreground='white', background='#008f82')
        style.map('Primary.TButton', background=[('active', '#007d72'), ('disabled', '#a3c9c4')])
        body = tk.Frame(self.root, bg='white', padx=28, pady=22)
        body.pack(fill='both', expand=True, padx=16, pady=16)
        tk.Label(body, text='追因', font=('Microsoft YaHei UI', 22, 'bold'), bg='white', fg='#163b3b').pack(anchor='w')
        self.state = tk.StringVar(value='正在启动，请稍候…')
        tk.Label(body, textvariable=self.state, bg='white', fg='#008f82', pady=14).pack(anchor='w')
        tk.Label(body, text='工作台地址（也可复制到浏览器打开）', bg='white', fg='#607575').pack(anchor='w')
        self.address = tk.StringVar(value='服务就绪后显示地址')
        ttk.Entry(body, textvariable=self.address, state='readonly').pack(fill='x', pady=(6, 14))
        actions = tk.Frame(body, bg='white')
        actions.pack(fill='x')
        self.open_button = ttk.Button(actions, text='打开工作台', style='Primary.TButton', command=self.open_browser, state='disabled')
        self.open_button.pack(side='left')
        self.copy_button = ttk.Button(actions, text='复制网址', command=self.copy_address, state='disabled')
        self.copy_button.pack(side='left', padx=8)
        ttk.Button(actions, text='查看启动日志', command=self.show_logs).pack(side='left')
        self.stop_button = ttk.Button(actions, text='退出追因', command=self.stop, state='disabled')
        self.stop_button.pack(side='right')
        self.detail = tk.StringVar(value='正在检查服务。此窗口会显示启动结果，不会调用模型。')
        tk.Label(body, textvariable=self.detail, bg='white', fg='#607575', wraplength=575, justify='left', pady=14).pack(anchor='w')
        tk.Label(body, text='关闭此窗口会保留后台服务；完全退出请点“退出追因”。', bg='white', fg='#607575').pack(anchor='w')
        self.root.after(80, self.drain)
        self.root.after(120, lambda: self.worker(self.start_service))

    def worker(self, function):
        threading.Thread(target=function, daemon=True).start()

    def start_service(self):
        try:
            url = launch.ensure_service(self.page, lambda text: self.messages.put(('status', text)))
            self.messages.put(('ready', url))
        except Exception:
            self.log_error()
            self.messages.put(('error', '启动未完成。点击“查看启动日志”可查看原因；无需反复双击。'))

    def open_browser(self):
        if not self.url:
            return
        self.detail.set('正在请求浏览器打开工作台；若未弹出，可用“复制网址”手动打开。')
        def open_now():
            try:
                method = launch.open_page(self.url)
                self.messages.put(('browser', method))
            except Exception:
                self.log_error()
                self.messages.put(('browser', 'not_opened'))
        self.worker(open_now)

    def copy_address(self):
        if self.url:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.url)
            self.detail.set('网址已复制，可粘贴到浏览器地址栏打开。')

    def show_logs(self):
        window = tk.Toplevel(self.root)
        window.title('追因 · 启动日志')
        window.geometry('780x430')
        tk.Label(window, text='日志位置：'+str(data_root()), anchor='w').pack(fill='x', padx=12, pady=8)
        viewer = tk.Text(window, wrap='word')
        viewer.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        for name in ('desktop-error.log', 'server-error.log', 'server.log'):
            file = data_root()/name
            viewer.insert('end', '\n—— '+name+' ——\n')
            try:
                viewer.insert('end', file.read_bytes()[-20000:].decode('utf-8', errors='replace') if file.is_file() else '尚无记录\n')
            except OSError:
                viewer.insert('end', '暂时无法读取\n')
        viewer.configure(state='disabled')

    def stop(self):
        self.stop_button.configure(state='disabled')
        self.detail.set('正在退出；如果仍有实验运行，会保留服务并提示。')
        def stop_now():
            try:
                from desktop_entry import stop_service
                self.messages.put(('stopped', stop_service()))
            except Exception:
                self.messages.put(('stop_error', '暂未退出：可能仍有实验运行，请到网页停止或等待完成后再试。'))
        self.worker(stop_now)

    @staticmethod
    def log_error():
        import traceback
        try:
            folder = data_root()
            folder.mkdir(parents=True, exist_ok=True)
            with (folder/'desktop-error.log').open('a', encoding='utf-8') as stream:
                traceback.print_exc(file=stream)
        except OSError:
            pass

    def probe(self, method):
        # Release automation checks the real Tk event loop and visible window,
        # without automating security dialogs or making model calls.
        if os.environ.get('ZHUIYIN_LAUNCHER_PROBE') != '1':
            return
        value = {'service_ready': bool(self.url), 'window_viewable': bool(self.root.winfo_viewable()),
                 'url': self.url, 'browser_method': method,
                 'open_enabled': str(self.open_button.cget('state')) == 'normal',
                 'copy_enabled': str(self.copy_button.cget('state')) == 'normal',
                 'stop_enabled': str(self.stop_button.cget('state')) == 'normal'}
        (data_root()/'desktop-window-check.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        self.root.after(250, self.root.destroy)

    def drain(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == 'status':
                    self.state.set(value)
                elif kind == 'ready':
                    self.url = value
                    self.state.set('● 服务已就绪')
                    self.address.set(value)
                    for button in (self.open_button, self.copy_button, self.stop_button):
                        button.configure(state='normal')
                    try:
                        (data_root()/'打开追因界面.html').write_text('<!doctype html><meta charset="utf-8"><title>打开追因</title>'
                            '<h1>追因工作台</h1><p>请保持追因后台运行，再打开下面地址：</p><p><a href="'+html.escape(value, quote=True)+'">'
                            +html.escape(value)+'</a></p>', encoding='utf-8')
                    except OSError:
                        pass
                    self.open_browser()
                elif kind == 'browser':
                    self.detail.set('已请求打开浏览器。未显示页面时，请点击“复制网址”，粘贴到浏览器打开。')
                    if value == 'not_opened':
                        self.detail.set('浏览器没有成功响应；服务正常，请复制上方网址手动打开。')
                    self.root.after(300, lambda method=value: self.probe(method))
                elif kind == 'error':
                    self.state.set('启动未完成')
                    self.detail.set(value)
                elif kind == 'stop_error':
                    self.detail.set(value)
                    self.stop_button.configure(state='normal')
                elif kind == 'stopped':
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self.drain)

    def run(self):
        self.root.mainloop()


def run_launcher(page='workbench'):
    LauncherWindow(page).run()
