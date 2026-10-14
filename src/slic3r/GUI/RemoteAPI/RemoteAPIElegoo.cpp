// src/slic3r/GUI/RemoteAPI/RemoteAPIElegoo.cpp
//
// elegoo-mcp: the Elegoo printers known to ElegooSlicer (Device tab) and their CANVAS
// multi-material unit, read-only:
//   GET /printers, /printers/{id}, /printers/{id}/status, /printers/{id}/tasks,
//   /printers/{id}/files, /printers/{id}/canvas
//
// Where the data comes from:
// - PrinterManager keeps every printer in a cache (PrinterCache) that the printers' own
//   status messages keep up to date; the printer routes read that cache. In a second
//   ElegooSlicer window, PrinterManager forwards these reads to the first window (IPC).
// - The CANVAS state is not cached by ElegooSlicer: every read is a request to the
//   printer, and the CC2 silently ignores requests for a while when it gets 3 or more in
//   a row. So this file keeps each printer's last CANVAS reply for a few seconds and lets
//   only one request per printer talk to it at a time (read_canvas).
//
// Threads: printer, IPC and SDK calls can block for seconds. They never run on the GUI
// thread: they run on a worker thread (run_on_worker) that the API thread waits for, up
// to a timeout (504 printer_timeout). GUI data (presets, app config) is read on the GUI
// thread with run_on_ui and handed to the worker.
//
// Secrets: a printer is written to JSON field by field from a whitelist (printer_json).
// Never put password, token, accessCode, pinCode, extraInfo, username, serialNumber,
// mainboardId or webUrl in a response or in a log line, and never pass on the printer
// layer's error messages (they can quote the printer id, see api_printer_id).
#include "RemoteAPIController.hpp"

#include "libslic3r/AppConfig.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/PrinterNetworkInfo.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/Utils/Elegoo/PrinterManager.hpp"
#include "slic3r/Utils/Elegoo/PrinterMmsManager.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

static const int PRINTER_TIMEOUT_S = 10; // reads of the printer list, status, tasks, files
static const int CANVAS_TIMEOUT_S  = 20; // a CANVAS read, including the wait for another one
static const int CANVAS_CACHE_S    = 5;  // a CANVAS reply is reused this long (CC2 rate limit)
static const int REFRESH_EVERY_S   = 2;  // at most one status refresh request per printer
static const int RAW_CACHE_S       = 2;  // at most one raw status read per printer (cloud)

// --- worker threads -----------------------------------------------------------------

// Runs fn on a new thread and waits up to timeout_s seconds for it to finish. Printer,
// IPC and SDK calls can block for a long time (a printer that does not answer, the
// cloud, the link to the first ElegooSlicer window), and they must not hold the API
// thread longer than the request's timeout, nor ever run on the GUI thread.
// On timeout this throws std::runtime_error("printer_timeout") (route_elegoo answers 504)
// and the thread is left to finish on its own: it is detached, and it owns everything it
// uses through the shared_ptr below, so nothing it touches goes away under it. fn hands
// its results back through state it captured by shared_ptr; read that state only after
// this function returned normally. An exception thrown by fn is rethrown here.
// (std::async is not used: the destructor of its future waits for the task, which would
// turn every timeout into a wait for the whole call.)
static void run_on_worker(std::function<void()> fn, int timeout_s)
{
    auto done = std::make_shared<std::promise<void>>();
    std::future<void> finished = done->get_future();
    std::thread([done, fn = std::move(fn)]() {
        try {
            fn();
            done->set_value();
        } catch (...) {
            done->set_exception(std::current_exception());
        }
    }).detach();
    if (finished.wait_for(std::chrono::seconds(std::max(timeout_s, 1))) != std::future_status::ready)
        throw std::runtime_error("printer_timeout");
    finished.get(); // rethrows what fn threw
}

// --- small helpers ------------------------------------------------------------------

static nlohmann::json string_or_null(const std::string &s)
{
    return s.empty() ? nlohmann::json() : nlohmann::json(s);
}

// Value of key in a query string "a=1&b=2" ("" when absent). Only used for plain
// numbers and flags, so no percent-decoding.
static std::string query_value(const std::string &query, const std::string &key)
{
    size_t start = 0;
    while (start <= query.size()) {
        size_t amp = query.find('&', start);
        std::string item = query.substr(start, amp == std::string::npos ? std::string::npos : amp - start);
        size_t eq = item.find('=');
        if (item.substr(0, eq) == key)
            return eq == std::string::npos ? std::string() : item.substr(eq + 1);
        if (amp == std::string::npos)
            break;
        start = amp + 1;
    }
    return std::string();
}

// Reads an optional integer query parameter: false when present but not a whole number.
static bool read_int_param(const std::string &query, const std::string &key, int &value)
{
    const std::string text = query_value(query, key);
    if (text.empty())
        return true; // absent: keep the default
    try {
        size_t used = 0;
        value = std::stoi(text, &used);
        return used == text.size();
    } catch (...) {
        return false;
    }
}

// The printer stack is closed while the window shuts down (MainFrame::shutdown closes it
// before it stops the API) and closed and opened again when the GUI is rebuilt. The API
// answers 503 then instead of reading a manager that is going away.
static Response printers_unavailable()
{
    return { 503, {{"error", "printers_unavailable"},
                   {"hint", "ElegooSlicer is closing or rebuilding its window; retry in a few seconds"}} };
}

// Turns a failed PrinterManager result into the API's answer. The printer layer's own
// message is left out on purpose: it can quote the printer id (see api_printer_id).
static Response printer_error(PrinterNetworkErrorCode code)
{
    switch (code) {
    case PrinterNetworkErrorCode::PRINTER_NOT_FOUND:
        return { 404, {{"error", "printer_not_found"}} };
    case PrinterNetworkErrorCode::PRINTER_CONNECTION_ERROR:
    case PrinterNetworkErrorCode::PRINTER_OFFLINE:
        return { 409, {{"error", "printer_offline"}} };
    case PrinterNetworkErrorCode::OPERATION_TIMEOUT:
        return { 504, {{"error", "printer_timeout"}} };
    case PrinterNetworkErrorCode::NOT_INITIALIZED:
    case PrinterNetworkErrorCode::PRINTER_NETWORK_NOT_INITIALIZED:
        return printers_unavailable();
    default:
        break;
    }
    // A second window that lost its link to the first one.
    if (code >= PrinterNetworkErrorCode::IPC_NOT_CONNECTED && code <= PrinterNetworkErrorCode::IPC_DEADLOCK_PREVENTED)
        return printers_unavailable();
    return { 502, {{"error", "printer_error"}, {"code", (int) code}} };
}

// --- printers -----------------------------------------------------------------------

// The id a printer has in the API. PrinterManager's own id is a random uuid for a
// printer added by hand, but the SDK builds the id of a discovered CC2 ("lan_" + serial
// number) and of a cloud printer ("cloud_" + serial number) from the serial number,
// which must not leave the API. So every printer is shown under an opaque id instead:
// "p" + 16 hex digits of a hash of its PrinterManager id. It stays the same for a printer
// across restarts. It hides the serial number from clients; it is not meant to resist a
// brute-force search for it.
static std::string api_printer_id(const std::string &printer_id)
{
    char text[24];
    std::snprintf(text, sizeof(text), "p%016llx", (unsigned long long) std::hash<std::string>{}(printer_id));
    return text;
}

// A host typed as a URL can carry a user name and password ("http://user:pass@host/"):
// keep only what follows the '@'.
static std::string host_without_credentials(const std::string &host)
{
    const size_t scheme = host.find("://");
    const size_t start  = scheme == std::string::npos ? 0 : scheme + 3;
    const size_t slash  = host.find('/', start);
    const size_t at     = host.rfind('@', slash == std::string::npos ? std::string::npos : slash);
    if (at == std::string::npos || at < start)
        return host;
    return host.substr(0, start) + host.substr(at + 1);
}

static const char *printer_status_name(PrinterStatus status)
{
    switch (status) {
    case PRINTER_STATUS_OFFLINE:             return "offline";
    case PRINTER_STATUS_IDLE:                return "idle";
    case PRINTER_STATUS_PRINTING:            return "printing";
    case PRINTER_STATUS_PAUSED:              return "paused";
    case PRINTER_STATUS_PAUSING:             return "pausing";
    case PRINTER_STATUS_CANCELED:            return "canceled";
    case PRINTER_STATUS_SELF_CHECKING:       return "self_checking";
    case PRINTER_STATUS_AUTO_LEVELING:       return "auto_leveling";
    case PRINTER_STATUS_PID_CALIBRATING:     return "pid_calibrating";
    case PRINTER_STATUS_RESONANCE_TESTING:   return "resonance_testing";
    case PRINTER_STATUS_UPDATING:            return "updating";
    case PRINTER_STATUS_FILE_COPYING:        return "file_copying";
    case PRINTER_STATUS_FILE_TRANSFERRING:   return "file_transferring";
    case PRINTER_STATUS_HOMING:              return "homing";
    case PRINTER_STATUS_PREHEATING:          return "preheating";
    case PRINTER_STATUS_FILAMENT_OPERATING:  return "filament_operating";
    case PRINTER_STATUS_EXTRUDER_OPERATING:  return "extruder_operating";
    case PRINTER_STATUS_PRINT_COMPLETED:     return "print_completed";
    case PRINTER_STATUS_RFID_RECOGNIZING:    return "rfid_recognizing";
    case PRINTER_STATUS_VIDEO_COMPOSING:     return "video_composing";
    case PRINTER_STATUS_EMERGENCY_STOP:      return "emergency_stop";
    case PRINTER_STATUS_POWER_LOSS_RECOVERY: return "power_loss_recovery";
    case PRINTER_STATUS_INITIALIZING:        return "initializing";
    case PRINTER_STATUS_BUSY:                return "busy";
    case PRINTER_STATUS_ERROR:               return "error";
    case PRINTER_STATUS_ID_NOT_MATCH:        return "id_not_match";
    case PRINTER_STATUS_AUTH_ERROR:          return "auth_error";
    default:                                 return "unknown";
    }
}

// The print in progress, or null. The cache clears the task when the printer is not
// printing, pausing or paused. The task name and start time are null: the printer layer
// keeps neither for the current task (only the cloud task history has them).
static nlohmann::json task_json(const PrinterPrintTask &task)
{
    if (task.taskId.empty() && task.fileName.empty())
        return nullptr;
    return {
        {"name", nullptr},
        {"file", string_or_null(task.fileName)},
        {"progress", task.progress},
        {"current_s", task.currentTime},
        {"total_s", task.totalTime},
        {"estimated_s", task.estimatedTime},
        {"begin_ts", nullptr},
    };
}

// One printer, built field by field (never from convertPrinterNetworkInfoToJson, which
// holds the secrets). A printer the slicer is not connected to is "offline", with no
// task: what the cache holds about it is old.
static nlohmann::json printer_json(const PrinterNetworkInfo &p)
{
    const bool          connected = p.connectStatus == PRINTER_CONNECT_STATUS_CONNECTED;
    const PrinterStatus status    = connected ? p.printerStatus : PRINTER_STATUS_OFFLINE;
    nlohmann::json j = nlohmann::json::object();
    j["id"]          = api_printer_id(p.printerId);
    j["name"]        = p.printerName;
    j["model"]       = p.printerModel;
    j["vendor"]      = p.vendor;
    j["host"]        = host_without_credentials(p.host);
    j["port"]        = p.port > 0 ? nlohmann::json(p.port) : nlohmann::json();
    j["network"]     = p.networkType == NETWORK_TYPE_WAN ? "wan" : "lan";
    j["firmware"]    = string_or_null(p.firmwareVersion);
    j["connected"]   = connected;
    j["status"]      = printer_status_name(status);
    j["status_code"] = (int) status;
    j["task"]        = connected ? task_json(p.printTask) : nlohmann::json();
    j["supports"]    = {
        {"canvas", p.systemCapabilities.supportsMultiFilament},
        {"timelapse", p.printCapabilities.supportsTimeLapse},
        {"auto_leveling", p.printCapabilities.supportsAutoBedLeveling},
        {"bed_switching", p.printCapabilities.supportsHeatedBedSwitching},
        {"auto_refill", p.printCapabilities.supportsAutoRefill},
        {"filament_mapping", p.printCapabilities.supportsFilamentMapping},
    };
    return j;
}

// Reads PrinterManager's printer list on a worker thread and finds the printer the API
// id names. False when there is none.
static bool find_printer(const std::string &api_id, PrinterNetworkInfo &found)
{
    auto printers = std::make_shared<std::vector<PrinterNetworkInfo>>();
    run_on_worker([printers] { *printers = PrinterManager::getInstance()->getPrinterList(); }, PRINTER_TIMEOUT_S);
    for (const PrinterNetworkInfo &p : *printers)
        if (api_printer_id(p.printerId) == api_id) {
            found = p;
            return true;
        }
    return false;
}

// GET /printers: every printer, and the one the send dialog would pick.
Response Controller::handle_get_printers()
{
    // Same inputs as PrintSendDialogEx: the project's printer model and the printer the
    // last print was sent to. Both are GUI data, read on the GUI thread.
    nlohmann::json pick = run_on_ui([]() -> nlohmann::json {
        std::string model;
        const auto *opt = wxGetApp().preset_bundle->printers.get_edited_preset().config.option<ConfigOptionString>("printer_model");
        if (opt != nullptr)
            model = opt->value;
        return {{"model", model}, {"recent", wxGetApp().app_config->get("recent", CONFIG_KEY_SELECTED_PRINTER_ID)}};
    });
    const std::string model  = pick["model"].get<std::string>();
    const std::string recent = pick["recent"].get<std::string>();

    auto printers = std::make_shared<std::vector<PrinterNetworkInfo>>();
    auto selected = std::make_shared<PrinterNetworkInfo>();
    run_on_worker([printers, selected, model, recent] {
        *printers = PrinterManager::getInstance()->getPrinterList();
        *selected = PrinterManager::getInstance()->getSelectedPrinter(model, recent);
    }, PRINTER_TIMEOUT_S);

    nlohmann::json list = nlohmann::json::array();
    for (const PrinterNetworkInfo &p : *printers)
        list.push_back(printer_json(p));
    nlohmann::json selected_id = selected->printerId.empty() ? nlohmann::json() : nlohmann::json(api_printer_id(selected->printerId));
    return { 200, {{"printers", list}, {"selected_id", selected_id}} };
}

// GET /printers/{id}
Response Controller::handle_get_printer(const std::string &api_id)
{
    PrinterNetworkInfo p;
    if (!find_printer(api_id, p))
        return { 404, {{"error", "printer_not_found"}} };
    return { 200, printer_json(p) };
}

// Asks the printer to send its status again (refreshPrinterStatus), at most once per
// REFRESH_EVERY_S per printer, so a client polling the status does not flood it (the CC2
// stops answering when asked too often). Fire-and-forget, as the Device tab does it: the
// printer's answer updates the cache, so the current reply still shows the cache as it
// is now (see age_s) and the next read sees the update. API thread only.
static void request_status_refresh(const PrinterNetworkInfo &p)
{
    if (p.connectStatus != PRINTER_CONNECT_STATUS_CONNECTED)
        return;
    static std::map<std::string, std::chrono::steady_clock::time_point> last_refresh;
    const auto now = std::chrono::steady_clock::now();
    auto it = last_refresh.find(p.printerId);
    if (it != last_refresh.end() && now - it->second < std::chrono::seconds(REFRESH_EVERY_S))
        return;
    last_refresh[p.printerId] = now;
    const std::string id = p.printerId;
    std::thread([id] { PrinterManager::getInstance()->refreshPrinterStatus(id); }).detach();
}

// The raw status of a cloud printer is the SDK's JSON as it came, not a whitelist, so
// every key that names a secret is dropped from it, at any depth and in any spelling
// ("serial_number", "accessCode", "sn", ...).
static bool is_secret_key(const std::string &key)
{
    std::string k;
    for (char c : key)
        if (c != '_' && c != '-')
            k += (char) std::tolower((unsigned char) c);
    static const char *secrets[] = {"password", "token", "accesscode", "pincode", "extrainfo",
                                    "username", "serial", "mainboard", "weburl"};
    for (const char *s : secrets)
        if (k.find(s) != std::string::npos)
            return true;
    return k == "sn";
}

static void drop_secret_keys(nlohmann::json &j)
{
    if (j.is_array()) {
        for (nlohmann::json &item : j)
            drop_secret_keys(item);
    } else if (j.is_object()) {
        for (auto it = j.begin(); it != j.end();) {
            if (is_secret_key(it.key())) {
                it = j.erase(it);
            } else {
                drop_secret_keys(it.value());
                ++it;
            }
        }
    }
}

// GET /printers/{id}/status[?raw=1]: the printer plus its current exception codes and
// the age of the cache. raw=1 adds the SDK's raw status JSON (cloud printers only).
Response Controller::handle_get_printer_status(const std::string &api_id, const std::string &query)
{
    const std::string raw_param = query_value(query, "raw");
    const bool        want_raw  = raw_param == "1" || raw_param == "true";
    PrinterNetworkInfo p;
    if (!find_printer(api_id, p))
        return { 404, {{"error", "printer_not_found"}} };
    // The SDK reads the raw status from Elegoo's cloud only; asking it about a LAN printer
    // would just fail (D14), so do not ask.
    if (want_raw && p.networkType != NETWORK_TYPE_WAN)
        return { 501, {{"error", "unsupported_on_lan"}} };

    request_status_refresh(p);
    nlohmann::json j = printer_json(p);
    // The exception codes the printer reports in its status (a CC2 on the LAN reports them
    // too). The printer layer keeps only the code; the cloud's exception history (with
    // level and time) is not read here.
    nlohmann::json exceptions = nlohmann::json::array();
    if (p.connectStatus == PRINTER_CONNECT_STATUS_CONNECTED)
        for (const PrinterExceptionDetail &e : p.exceptions)
            exceptions.push_back({{"code", e.code}, {"level", nullptr}, {"time", nullptr}});
    j["exceptions"] = exceptions;
    // Seconds since the cache last heard from or about the printer.
    if (p.lastActiveTime > 0) {
        const long long now = (long long) std::chrono::duration_cast<std::chrono::seconds>(
                                  std::chrono::system_clock::now().time_since_epoch()).count();
        j["age_s"] = std::max(0LL, now - (long long) p.lastActiveTime);
    } else {
        j["age_s"] = nullptr;
    }

    if (want_raw) {
        // At most one read per RAW_CACHE_S per printer; in between, the last one is
        // returned with its age. API thread only.
        struct RawStatus { std::chrono::steady_clock::time_point read_at; nlohmann::json raw; };
        static std::map<std::string, RawStatus> last_raw;
        const auto now = std::chrono::steady_clock::now();
        auto it = last_raw.find(p.printerId);
        if (it == last_raw.end() || now - it->second.read_at >= std::chrono::seconds(RAW_CACHE_S)) {
            auto result = std::make_shared<PrinterNetworkResult<std::string>>();
            const std::string id = p.printerId;
            run_on_worker([result, id] { *result = PrinterManager::getInstance()->getPrinterStatusRaw(id); }, PRINTER_TIMEOUT_S);
            if (!result->isSuccess() || !result->hasData())
                return printer_error(result->isSuccess() ? PrinterNetworkErrorCode::PRINTER_INVALID_RESPONSE : result->code);
            nlohmann::json raw = nlohmann::json::parse(result->value(), nullptr, /*allow_exceptions=*/false);
            if (raw.is_discarded())
                return printer_error(PrinterNetworkErrorCode::PRINTER_INVALID_RESPONSE);
            drop_secret_keys(raw);
            it = last_raw.insert_or_assign(p.printerId, RawStatus{std::chrono::steady_clock::now(), raw}).first;
        }
        j["raw"]       = it->second.raw;
        j["raw_age_s"] = (long long) std::chrono::duration_cast<std::chrono::seconds>(
                             std::chrono::steady_clock::now() - it->second.read_at).count();
    }
    return { 200, j };
}

// GET /printers/{id}/tasks and /files: the print history and the files stored on a cloud
// printer, one page at a time (page from 1, size 1..100).
Response Controller::handle_get_printer_history(const std::string &api_id, const std::string &what, const std::string &query)
{
    int page = 1;
    int size = 20;
    if (!read_int_param(query, "page", page) || page < 1)
        return { 400, {{"error", "bad_page"}, {"detail", "page: a whole number from 1"}} };
    if (!read_int_param(query, "size", size) || size < 1 || size > 100)
        return { 400, {{"error", "bad_size"}, {"detail", "size: a whole number from 1 to 100"}} };
    PrinterNetworkInfo p;
    if (!find_printer(api_id, p))
        return { 404, {{"error", "printer_not_found"}} };
    // In the SDK the task list and the file list come from Elegoo's cloud only; for a LAN
    // printer the call would fail or go to the cloud, so it is not made (D14).
    if (p.networkType != NETWORK_TYPE_WAN)
        return { 501, {{"error", "unsupported_on_lan"}} };

    const std::string id    = p.printerId;
    nlohmann::json    items = nlohmann::json::array();
    // Thumbnails are left out: they are links into Elegoo's cloud storage.
    if (what == "tasks") {
        auto result = std::make_shared<PrinterNetworkResult<PrinterPrintTaskResponse>>();
        run_on_worker([result, id, page, size] { *result = PrinterManager::getInstance()->getPrintTaskList(id, page, size); },
                      PRINTER_TIMEOUT_S);
        if (!result->isSuccess() || !result->hasData())
            return printer_error(result->isSuccess() ? PrinterNetworkErrorCode::PRINTER_INVALID_RESPONSE : result->code);
        for (const PrinterPrintTask &t : result->value().taskList)
            items.push_back({{"id", t.taskId},
                             {"name", t.taskName},
                             {"begin_ts", t.beginTime},
                             {"end_ts", t.endTime},
                             {"status_code", t.taskStatus}});
        return { 200, {{"items", items}, {"total", result->value().totalTasks}} };
    }
    auto result = std::make_shared<PrinterNetworkResult<PrinterPrintFileResponse>>();
    run_on_worker([result, id, page, size] { *result = PrinterManager::getInstance()->getFileList(id, page, size); },
                  PRINTER_TIMEOUT_S);
    if (!result->isSuccess() || !result->hasData())
        return printer_error(result->isSuccess() ? PrinterNetworkErrorCode::PRINTER_INVALID_RESPONSE : result->code);
    for (const PrinterPrintFile &f : result->value().fileList)
        items.push_back({{"name", f.fileName},
                         {"size_bytes", f.size},
                         {"print_time_s", f.printTime},
                         {"layers", f.layer},
                         {"layer_height_mm", f.layerHeight},
                         {"created_ts", f.createTime},
                         {"filament_g", f.totalFilamentUsed},
                         {"filament_mm", f.totalFilamentUsedLength},
                         {"print_count", f.totalPrintTimes},
                         {"last_print_ts", f.lastPrintTime}});
    return { 200, {{"items", items}, {"total", result->value().totalFiles}} };
}

// --- route group --------------------------------------------------------------------

// Every /api/v1/printers route (dispatch sends the whole prefix here).
Response Controller::route_elegoo(const Request &req)
{
    try {
        if (wxGetApp().is_closing())
            return printers_unavailable();
        const size_t      qpos  = req.target.find('?');
        const std::string path  = req.target.substr(0, qpos);
        const std::string query = qpos == std::string::npos ? std::string() : req.target.substr(qpos + 1);

        if (req.method == "GET" && path == "/api/v1/printers")
            return handle_get_printers();
        // /api/v1/printers/<id> and /api/v1/printers/<id>/<action>
        static const std::string pfx = "/api/v1/printers/";
        if (path.size() > pfx.size() && path.compare(0, pfx.size(), pfx) == 0) {
            const std::string rest   = path.substr(pfx.size());
            const size_t      slash  = rest.find('/');
            const std::string id     = rest.substr(0, slash);
            const std::string action = slash == std::string::npos ? std::string() : rest.substr(slash + 1);
            if (req.method == "GET" && action.empty())
                return handle_get_printer(id);
            if (req.method == "GET" && action == "status")
                return handle_get_printer_status(id, query);
            if (req.method == "GET" && (action == "tasks" || action == "files"))
                return handle_get_printer_history(id, action, query);
        }
        return { 404, {{"error", "not_found"}} };
    } catch (const std::runtime_error &e) {
        if (std::string(e.what()) == "printer_timeout")
            return { 504, {{"error", "printer_timeout"}} };
        throw; // ui_timeout and the rest are answered by dispatch
    }
}

}}} // namespace Slic3r::GUI::RemoteAPI
