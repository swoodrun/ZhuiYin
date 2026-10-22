// Native entry point: select writable app-local TEMP before PyInstaller runs.
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <wincrypt.h>
#include <string>
#include <vector>
#include <stdexcept>
#include "payload_config.h"

static void ensure(bool ok) { if (!ok) throw std::runtime_error("Windows operation failed"); }

class Sha256 {
    HCRYPTPROV provider = 0;
    HCRYPTHASH hash = 0;
public:
    Sha256() {
        ensure(CryptAcquireContextW(&provider, nullptr, nullptr, PROV_RSA_AES, CRYPT_VERIFYCONTEXT));
        if (!CryptCreateHash(provider, CALG_SHA_256, 0, 0, &hash)) {
            CryptReleaseContext(provider, 0); provider = 0; ensure(false);
        }
    }
    ~Sha256() { if (hash) CryptDestroyHash(hash); if (provider) CryptReleaseContext(provider, 0); }
    void add(const BYTE* data, DWORD size) { ensure(CryptHashData(hash, data, size, 0)); }
    std::string finish() {
        BYTE digest[32]; DWORD size = sizeof(digest);
        ensure(CryptGetHashParam(hash, HP_HASHVAL, digest, &size, 0));
        const char* hex = "0123456789abcdef"; std::string result;
        for (BYTE value : digest) { result += hex[value >> 4]; result += hex[value & 15]; }
        return result;
    }
};

static std::string fileHash(const std::wstring& path) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (file == INVALID_HANDLE_VALUE) return "";
    try {
        Sha256 hash; BYTE bytes[65536]; DWORD read;
        do { ensure(ReadFile(file, bytes, sizeof(bytes), &read, nullptr)); hash.add(bytes, read); } while (read);
        CloseHandle(file); return hash.finish();
    } catch (...) { CloseHandle(file); throw; }
}

static void directory(const std::wstring& path) {
    if (!CreateDirectoryW(path.c_str(), nullptr)) {
        ensure(GetLastError() == ERROR_ALREADY_EXISTS &&
               (GetFileAttributesW(path.c_str()) & FILE_ATTRIBUTE_DIRECTORY));
    }
}

static std::wstring quote(const std::wstring& value) {
    std::wstring result = L"\""; size_t slashes = 0;
    for (wchar_t ch : value) {
        if (ch == L'\\') { ++slashes; continue; }
        result.append(ch == L'\"' ? slashes * 2 + 1 : slashes, L'\\');
        result += ch; slashes = 0;
    }
    result.append(slashes * 2, L'\\'); result += L'\"'; return result;
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    HANDLE mutex = nullptr; bool owned = false;
    try {
        std::vector<wchar_t> name(32768);
        DWORD length = GetModuleFileNameW(nullptr, name.data(), (DWORD)name.size());
        ensure(length > 0 && length < name.size());
        std::wstring path(name.data(), length);
        std::wstring root = path.substr(0, path.find_last_of(L"\\/"));
        std::wstring cache = root + L"\\追因运行缓存";
        directory(cache);
        std::wstring version = cache + L"\\" + PAYLOAD_SHORT_W;
        directory(version);
        std::wstring temp = version + L"\\temp";
        directory(temp);
        // Set these before loading any Python or its one-file bootloader.
        ensure(SetEnvironmentVariableW(L"TEMP", temp.c_str()));
        ensure(SetEnvironmentVariableW(L"TMP", temp.c_str()));
        if (GetEnvironmentVariableW(L"ZHUIYIN_DATA_DIR", nullptr, 0) == 0)
            ensure(SetEnvironmentVariableW(L"ZHUIYIN_DATA_DIR", (root + L"\\追因数据").c_str()));
        ensure(SetEnvironmentVariableW(L"PYINSTALLER_RESET_ENVIRONMENT", L"1"));
        std::wstring core = version + L"\\core.exe";
        mutex = CreateMutexW(nullptr, FALSE, L"Local\\ZhuiyinPayload-" PAYLOAD_SHORT_W);
        ensure(mutex != nullptr);
        DWORD wait = WaitForSingleObject(mutex, 30000);
        ensure(wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED); owned = true;
        if (fileHash(core) != PAYLOAD_SHA256) {
            HRSRC resource = FindResourceW(nullptr, MAKEINTRESOURCEW(101), RT_RCDATA);
            ensure(resource != nullptr);
            DWORD size = SizeofResource(nullptr, resource);
            HGLOBAL loaded = LoadResource(nullptr, resource);
            const BYTE* bytes = (const BYTE*)LockResource(loaded);
            ensure(bytes != nullptr && size > 0);
            Sha256 hash; hash.add(bytes, size); ensure(hash.finish() == PAYLOAD_SHA256);
            std::wstring staged = core + L"." + std::to_wstring(GetCurrentProcessId()) + L".new";
            HANDLE output = CreateFileW(staged.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
            ensure(output != INVALID_HANDLE_VALUE);
            DWORD written = 0; bool ok = WriteFile(output, bytes, size, &written, nullptr) && written == size;
            if (ok) ok = FlushFileBuffers(output);
            CloseHandle(output); ensure(ok);
            if (!MoveFileExW(staged.c_str(), core.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
                DeleteFileW(staged.c_str()); ensure(false);
            }
        }
        ReleaseMutex(mutex); owned = false; CloseHandle(mutex); mutex = nullptr;
        int count = 0; LPWSTR* arguments = CommandLineToArgvW(GetCommandLineW(), &count);
        ensure(arguments != nullptr);
        std::wstring command = quote(core);
        for (int i = 1; i < count; ++i) command += L" " + quote(arguments[i]);
        LocalFree(arguments);
        std::vector<wchar_t> mutableCommand(command.begin(), command.end()); mutableCommand.push_back(0);
        STARTUPINFOW startup = {}; startup.cb = sizeof(startup);
        PROCESS_INFORMATION child = {};
        ensure(CreateProcessW(core.c_str(), mutableCommand.data(), nullptr, nullptr, FALSE,
                              CREATE_NO_WINDOW, nullptr, root.c_str(), &startup, &child));
        CloseHandle(child.hThread);
        WaitForSingleObject(child.hProcess, INFINITE);
        DWORD code = 1; GetExitCodeProcess(child.hProcess, &code); CloseHandle(child.hProcess);
        return (int)code;
    } catch (...) {
        if (owned) ReleaseMutex(mutex);
        if (mutex) CloseHandle(mutex);
        MessageBoxW(nullptr, L"追因无法准备运行文件。\n请把程序放在可写文件夹（例如桌面或下载目录）后重试。\n程序不会修改系统临时目录设置，也不会删除实验历史。", L"追因启动失败", MB_ICONERROR);
        return 1;
    }
}
