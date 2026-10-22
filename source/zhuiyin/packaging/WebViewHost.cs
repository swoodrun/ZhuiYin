using System;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Collections.Generic;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed class ZhuiyinWindow : Form
{
    const string AppUserModelId = "Zhuiyin.Desktop";
    const int WmGetIcon = 0x007F;
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern int SetCurrentProcessExplicitAppUserModelID(string appId);
    [DllImport("shell32.dll")]
    static extern int GetCurrentProcessExplicitAppUserModelID(out IntPtr appId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr SendMessage(IntPtr window, int message, IntPtr parameter, IntPtr extra);
    readonly string home, data, origin;
    readonly string cache;
    readonly string iconPath;
    readonly Icon applicationIcon;
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly WebView2 view = new WebView2();
    readonly ToolStripStatusLabel status = new ToolStripStatusLabel("正在载入工作台…");
    bool closing, allowClose, probeStarted;
    int downloads;
    Dictionary<string, object> observation;
    public ZhuiyinWindow(string url, string folder)
    {
        var uri = new Uri(url);
        if (uri.Scheme != "http" || uri.Host != "127.0.0.1" || uri.Port < 8766 || uri.Port > 8775 || uri.UserInfo.Length != 0)
            throw new ArgumentException("本机工作台地址无效");
        home = url; data = folder; origin = uri.GetLeftPart(UriPartial.Authority);
        Text = "追因"; StartPosition = FormStartPosition.CenterScreen;
        iconPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "zhuiyin.ico");
        applicationIcon = new Icon(iconPath);
        Icon = applicationIcon; ShowIcon = true; ShowInTaskbar = true;
        AutoScaleMode = AutoScaleMode.Dpi;
        Rectangle area = Screen.PrimaryScreen.WorkingArea;
        Size = new Size(Math.Min(1280, area.Width - 60), Math.Min(860, area.Height - 60));
        MinimumSize = new Size(800, 600);
        WindowState = FormWindowState.Maximized;
        Font = new Font("Microsoft YaHei UI", 9F);
        var bar = new StatusStrip(); bar.Items.Add(status);
        view.Dock = DockStyle.Fill;
        cache = Path.Combine(data, "webview2");
        Directory.CreateDirectory(cache);
        string temporary = Path.Combine(data, "runtime-temp");
        Directory.CreateDirectory(temporary);
        Environment.SetEnvironmentVariable("TEMP", temporary);
        Environment.SetEnvironmentVariable("TMP", temporary);
        Log("host="+Application.ExecutablePath+" cache="+cache+" temp="+Path.GetTempPath());
        view.CreationProperties = new CoreWebView2CreationProperties { UserDataFolder = cache, IsInPrivateModeEnabled = true };
        Controls.Add(view); Controls.Add(bar);
        Shown += async delegate { await Initialize(); };
        FormClosing += CloseWindow;
        FormClosed += delegate { view.Dispose(); applicationIcon.Dispose(); };
    }
    void Log(string text) { try { File.AppendAllText(Path.Combine(data, "native-startup.log"), DateTime.UtcNow.ToString("o")+" "+text+Environment.NewLine, Encoding.UTF8); } catch { } }
    static string Sha256(byte[] bytes)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
    }
    static string IconBitmapSha256(Icon icon)
    {
        using (var bitmap = icon.ToBitmap())
        using (var stream = new MemoryStream()) {
            bitmap.Save(stream, System.Drawing.Imaging.ImageFormat.Png);
            return Sha256(stream.ToArray());
        }
    }
    Dictionary<string, object> NativeIconObservation()
    {
        IntPtr identity;
        Marshal.ThrowExceptionForHR(GetCurrentProcessExplicitAppUserModelID(out identity));
        string currentId;
        try { currentId = Marshal.PtrToStringUni(identity); }
        finally { Marshal.FreeCoTaskMem(identity); }
        var result = new Dictionary<string, object> {
            {"app_user_model_id", currentId}, {"show_in_taskbar", ShowInTaskbar},
            {"icon_file", iconPath}, {"icon_file_sha256", Sha256(File.ReadAllBytes(iconPath))},
            {"loaded_icon_bitmap_sha256", IconBitmapSha256(applicationIcon)}
        };
        Func<Bitmap, string> pixelHash = delegate(Bitmap bitmap) {
            byte[] pixels = new byte[bitmap.Width * bitmap.Height * 4];
            int offset = 0;
            for (int y = 0; y < bitmap.Height; y++)
                for (int x = 0; x < bitmap.Width; x++) {
                    Color pixel = bitmap.GetPixel(x, y);
                    pixels[offset++] = pixel.A;
                    pixels[offset++] = pixel.A == 0 ? (byte)0 : pixel.R;
                    pixels[offset++] = pixel.A == 0 ? (byte)0 : pixel.G;
                    pixels[offset++] = pixel.A == 0 ? (byte)0 : pixel.B;
                }
            return Sha256(pixels);
        };
        result["comparison"] = "same-size approved ICO frame rendered through native HICON; visible ARGB pixels";
        foreach (var pair in new Dictionary<string, int> {{"big", 1}, {"small", 0}}) {
            IntPtr nativeIcon = SendMessage(Handle, WmGetIcon, new IntPtr(pair.Value), IntPtr.Zero);
            result[pair.Key+"_handle_present"] = nativeIcon != IntPtr.Zero;
            result[pair.Key+"_matches_loaded_icon"] = false;
            if (nativeIcon != IntPtr.Zero) {
                using (var snapshot = (Icon)System.Drawing.Icon.FromHandle(nativeIcon).Clone())
                using (var actual = snapshot.ToBitmap())
                using (var approved = new Icon(iconPath, actual.Size))
                using (var approvedNative = (Icon)System.Drawing.Icon.FromHandle(approved.Handle).Clone())
                using (var expected = approvedNative.ToBitmap()) {
                    result[pair.Key+"_bitmap_sha256"] = IconBitmapSha256(snapshot);
                    result[pair.Key+"_size"] = new int[] { actual.Width, actual.Height };
                    result[pair.Key+"_expected_size"] = new int[] { expected.Width, expected.Height };
                    string actualPixels = pixelHash(actual), expectedPixels = pixelHash(expected);
                    result[pair.Key+"_pixel_sha256"] = actualPixels;
                    result[pair.Key+"_expected_pixel_sha256"] = expectedPixels;
                    result[pair.Key+"_matches_loaded_icon"] = actual.Size == expected.Size && actualPixels == expectedPixels;
                    actual.Save(Path.Combine(data, "native-icon-"+pair.Key+".png"), System.Drawing.Imaging.ImageFormat.Png);
                    expected.Save(Path.Combine(data, "native-icon-"+pair.Key+"-approved.png"), System.Drawing.Imaging.ImageFormat.Png);
                }
            }
        }
        return result;
    }
    bool Local(string url)
    {
        if (url == "about:blank" || url.StartsWith("data:image/", StringComparison.OrdinalIgnoreCase)) return true;
        if (url.StartsWith("blob:", StringComparison.OrdinalIgnoreCase)) url = url.Substring(5);
        Uri target;
        return Uri.TryCreate(url, UriKind.Absolute, out target) && target.UserInfo.Length == 0 &&
            target.GetLeftPart(UriPartial.Authority).Equals(origin, StringComparison.OrdinalIgnoreCase);
    }
    async Task Initialize()
    {
        try {
            Log("runtime="+CoreWebView2Environment.GetAvailableBrowserVersionString());
            await view.EnsureCoreWebView2Async();
            Log("controller ready");
            var core = view.CoreWebView2;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsPasswordAutosaveEnabled = false;
            core.Settings.IsGeneralAutofillEnabled = false;
            string downloadFolder = Path.Combine(data, "downloads");
            Directory.CreateDirectory(downloadFolder);
            core.Profile.DefaultDownloadFolderPath = downloadFolder;
            core.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e) {
                e.Handled = true; if (Local(e.Uri)) core.Navigate(e.Uri);
            };
            core.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs e) {
                Log("navigation starting "+e.Uri);
                if (!Local(e.Uri)) { e.Cancel = true; status.Text = "本窗口仅打开追因本机页面"; }
            };
            core.ProcessFailed += delegate(object sender, CoreWebView2ProcessFailedEventArgs e) { Log("process failed "+e.ProcessFailedKind+" "+e.Reason); };
            core.PermissionRequested += delegate(object sender, CoreWebView2PermissionRequestedEventArgs e) {
                e.State = CoreWebView2PermissionState.Deny;
            };
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += delegate(object sender, CoreWebView2WebResourceRequestedEventArgs e) {
                if (!Local(e.Request.Uri))
                    e.Response = core.Environment.CreateWebResourceResponse(new MemoryStream(new byte[0]), 403, "Local application only", "Content-Type: text/plain");
            };
            core.DownloadStarting += Download;
            core.NavigationCompleted += async delegate(object sender, CoreWebView2NavigationCompletedEventArgs e) {
                Log("navigation completed success="+e.IsSuccess+" error="+e.WebErrorStatus);
                if (e.IsSuccess) {
                    status.Text = "本机服务已就绪 · 所有操作在软件内进行";
                    if (Environment.GetEnvironmentVariable("ZHUIYIN_EMBEDDED_PROBE") == "1" && !probeStarted) {
                        probeStarted = true; await Task.Delay(1200); await Probe();
                    }
                } else if (!probeStarted) status.Text = "页面暂时未能载入，请关闭后重试；日志位于追因数据目录";
            };
            Log("navigate "+home+" size="+view.ClientSize);
            core.Navigate(home);
        } catch (Exception ex) {
            Environment.ExitCode = 1;
            Log("initialization failed "+ex.ToString());
            try { File.AppendAllText(Path.Combine(data, "native-error.log"), ex.ToString()+Environment.NewLine, Encoding.UTF8); } catch { }
            status.Text = "页面启动失败；错误详情已显示在窗口内。";
            var details = new TextBox { Multiline = true, ReadOnly = true, Dock = DockStyle.Fill,
                ScrollBars = ScrollBars.Both, Font = new Font("Microsoft YaHei UI", 10F),
                Text = "追因页面暂未能载入\r\n\r\n请确认解压目录及追因数据目录可以写入。此错误不表示模型调用失败。\r\n"+
                    "若系统没有安装 Microsoft Edge WebView2 Runtime，请安装后重新启动。\r\n\r\n"+
                    "日志："+Path.Combine(data, "native-error.log")+"\r\n\r\n"+ex.ToString() };
            Controls.Add(details); details.BringToFront();
            if (Environment.GetEnvironmentVariable("ZHUIYIN_EMBEDDED_PROBE") == "1") Close();
        }
    }
    void Download(object sender, CoreWebView2DownloadStartingEventArgs e)
    {
        Log("download starting "+e.DownloadOperation.Uri);
        e.Handled = true;
        string target;
        if (Environment.GetEnvironmentVariable("ZHUIYIN_EMBEDDED_PROBE") == "1")
            target = Path.Combine(data, "embedded-probe-download.csv");
        else using (var dialog = new SaveFileDialog()) {
            dialog.Title = "保存追因结果";
            dialog.FileName = Path.GetFileName(e.ResultFilePath);
            dialog.Filter = "所有文件|*.*";
            if (dialog.ShowDialog(this) != DialogResult.OK) { e.Cancel = true; return; }
            target = dialog.FileName;
        }
        e.ResultFilePath = target;
        downloads++;
        var operation = e.DownloadOperation;
        bool done = false;
        operation.StateChanged += async delegate {
            if (!done && operation.State != CoreWebView2DownloadState.InProgress) {
                done = true; downloads--;
                if (operation.State == CoreWebView2DownloadState.Completed) {
                    status.Text = "已保存："+target;
                    if (observation != null) await FinishProbe(target);
                } else status.Text = "下载未完成，请检查保存位置后重试";
            }
        };
    }
    async Task<object> LayoutObservation()
    {
        string script = @"JSON.stringify((()=>{const box=s=>{const e=document.querySelector(s),r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}};return {viewport:{width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth},columns:getComputedStyle(document.querySelector('.workbench-columns')).gridTemplateColumns,left:box('.workbench-columns>.agent-setup'),right:box('.workbench-task'),form:box('#experiment-form'),button:box('#run-btn'),logo:document.querySelector('img.brand-mark')?.complete&&document.querySelector('img.brand-mark')?.naturalWidth===512,favicon:!!document.querySelector('link[rel=icon][href=""/assets/favicon.ico""]')}})())";
        var result = (Dictionary<string, object>)json.DeserializeObject(json.Deserialize<string>(await view.CoreWebView2.ExecuteScriptAsync(script)));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#experiment-form>.run-actions>.action-menu>summary').click()");
        string menuScript = "JSON.stringify((()=>{const e=document.querySelector('#experiment-form>.run-actions .menu-content'),r=e.getBoundingClientRect();return {visible:getComputedStyle(e).display!=='none'&&r.width>0,top:r.top,bottom:r.bottom}})())";
        result["more_menu"] = json.DeserializeObject(json.Deserialize<string>(await view.CoreWebView2.ExecuteScriptAsync(menuScript)));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#experiment-form>.run-actions>.action-menu>summary').click()");
        return result;
    }
    async Task Capture(string name)
    {
        using (var stream = File.Create(Path.Combine(data, name)))
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
    }
    async Task Probe()
    {
        Log("probe start");
        string script = "JSON.stringify({title:document.title,ready:document.querySelector('#health-label')?.textContent,workbench:!!document.querySelector('#experiment-form'),fileInputs:document.querySelectorAll('input[type=file]').length,batchMultiple:document.querySelector('#test-files')?.multiple})";
        string raw = await view.CoreWebView2.ExecuteScriptAsync(script);
        Log("probe dom="+raw);
        string decoded = json.Deserialize<string>(raw);
        observation = new Dictionary<string, object> {
            {"window_visible", Visible}, {"engine", "WebView2"},
            {"off_the_record", view.CoreWebView2.Profile.IsInPrivateModeEnabled},
            {"external_browser_requested", false}, {"url", view.Source.AbsoluteUri},
            {"browser_process_id", view.CoreWebView2.BrowserProcessId},
            {"native_icons", NativeIconObservation()},
            {"dom", json.DeserializeObject(decoded)}
        };
        var pages = new Dictionary<string, object>();
        foreach (string page in new string[] { "testing", "import", "statistics", "history", "usage", "workbench" }) {
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('[data-page-link=\""+page+"\"]').click()");
            await Task.Delay(150);
            string result = await view.CoreWebView2.ExecuteScriptAsync("JSON.stringify({hash:location.hash,visible:getComputedStyle(document.querySelector('[data-page=\""+page+"\"]')).display!=='none',active:document.querySelector('[data-page-link=\""+page+"\"]').getAttribute('aria-current')==='page'})");
            pages[page] = json.DeserializeObject(json.Deserialize<string>(result));
        }
        observation["page_clicks"] = pages;
        var layouts = new Dictionary<string, object>();
        layouts["desktop_100"] = await LayoutObservation();
        await Capture("workbench-100.png");
        view.ZoomFactor = 1.25;
        await Task.Delay(300);
        layouts["desktop_125"] = await LayoutObservation();
        await Capture("workbench-125.png");
        view.ZoomFactor = 1;
        await view.CoreWebView2.CallDevToolsProtocolMethodAsync("Emulation.setDeviceMetricsOverride", "{\"width\":390,\"height\":844,\"deviceScaleFactor\":1,\"mobile\":true}");
        await Task.Delay(300);
        layouts["narrow_390"] = await LayoutObservation();
        await Capture("workbench-narrow.png");
        await view.CoreWebView2.CallDevToolsProtocolMethodAsync("Emulation.clearDeviceMetricsOverride", "{}");
        await Task.Delay(200);
        observation["layouts"] = layouts;
        using (var stream = File.Create(Path.Combine(data, "embedded-window.png")))
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
        File.WriteAllText(Path.Combine(data, "embedded-render.json"), json.Serialize(observation), Encoding.UTF8);
        view.CoreWebView2.Navigate(origin+"/api/templates/scenarios.csv");
    }
    async Task FinishProbe(string path)
    {
        observation["download_bytes"] = new FileInfo(path).Length;
        using (var sha = SHA256.Create()) observation["download_sha256"] = BitConverter.ToString(sha.ComputeHash(File.ReadAllBytes(path))).Replace("-", "").ToLowerInvariant();
        using (var stream = File.Create(Path.Combine(data, "embedded-window.png")))
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
        File.WriteAllText(Path.Combine(data, "embedded-check.json"), json.Serialize(observation), Encoding.UTF8);
        observation = null;
        await Task.Delay(300); Close();
    }
    bool StopService()
    {
        string record = Path.Combine(data, "desktop-session.json");
        if (!File.Exists(record)) return true;
        var session = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(record));
        int port = Convert.ToInt32(session["port"]);
        if (port != new Uri(home).Port) return false;
        string body = json.Serialize(new Dictionary<string, object> {{"token", session["token"]}});
        var request = (HttpWebRequest)WebRequest.Create(origin+"/api/desktop/stop");
        request.Proxy = null; request.Method = "POST"; request.ContentType = "application/json"; request.Timeout = 5000;
        byte[] bytes = Encoding.UTF8.GetBytes(body); request.ContentLength = bytes.Length;
        using (var stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
        try { using (var response = (HttpWebResponse)request.GetResponse()) return response.StatusCode == HttpStatusCode.OK; }
        catch (WebException ex) {
            var response = ex.Response as HttpWebResponse;
            if (response != null && response.StatusCode == HttpStatusCode.Conflict) return false;
            return !File.Exists(record);
        }
    }
    async void CloseWindow(object sender, FormClosingEventArgs e)
    {
        if (allowClose) return;
        e.Cancel = true;
        if (closing) return;
        if (downloads > 0) { MessageBox.Show(this, "请等待结果下载完成后再退出。", "追因"); return; }
        closing = true; status.Text = "正在退出本机服务…";
        bool stopped = false;
        try { stopped = await Task.Run((Func<bool>)StopService); } catch { }
        if (stopped) { allowClose = true; Close(); }
        else { closing = false; status.Text = "实验仍在运行，请先在页面停止或等待完成后退出。"; MessageBox.Show(this, status.Text, "暂未退出追因"); }
    }
    [STAThread]
    static void Main(string[] args)
    {
        if (args.Length != 2) return;
        try {
            Marshal.ThrowExceptionForHR(SetCurrentProcessExplicitAppUserModelID(AppUserModelId));
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            string id;
            using (var sha = SHA256.Create()) id = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(Path.GetFullPath(args[1]).ToLowerInvariant()))).Replace("-", "");
            string name = "Local\\Zhuiyin.Window."+id;
            using (var mutex = new Mutex(false, name)) {
                bool owns;
                try { owns = mutex.WaitOne(0); } catch (AbandonedMutexException) { owns = true; }
                if (!owns) {
                    try { using (var active = EventWaitHandle.OpenExisting(name+".activate")) active.Set(); } catch { }
                    return;
                }
                try {
                    using (var active = new EventWaitHandle(false, EventResetMode.AutoReset, name+".activate")) {
                        var window = new ZhuiyinWindow(args[0], args[1]);
                        var listener = new Thread(delegate() {
                            try { while (active.WaitOne()) window.BeginInvoke((Action)delegate {
                                if (window.WindowState == FormWindowState.Minimized) window.WindowState = FormWindowState.Normal;
                                window.Show(); window.Activate();
                            }); } catch (ObjectDisposedException) { } catch (InvalidOperationException) { }
                        });
                        listener.IsBackground = true; listener.Start();
                        Application.Run(window);
                    }
                } finally { mutex.ReleaseMutex(); }
            }
        }
        catch (Exception ex) {
            Directory.CreateDirectory(args[1]);
            File.AppendAllText(Path.Combine(args[1], "native-error.log"), ex.ToString(), Encoding.UTF8);
            MessageBox.Show("内嵌窗口启动失败，请查看追因数据中的native-error.log。", "追因");
        }
    }
}
