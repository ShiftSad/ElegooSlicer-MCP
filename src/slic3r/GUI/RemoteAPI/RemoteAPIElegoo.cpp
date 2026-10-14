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

// --- CANVAS -------------------------------------------------------------------------

namespace {

// The last CANVAS reply of each printer, keyed by PrinterManager's printer id. The API
// thread and the worker threads both use it, always under `mutex`.
struct CanvasEntry
{
    bool                                  fetching { false };  // a worker is asking the printer now
    bool                                  has_reply { false };
    std::chrono::steady_clock::time_point replied_at;
    PrinterNetworkErrorCode               code { PrinterNetworkErrorCode::SUCCESS };
    PrinterMmsGroup                       group {};            // the reply, when code is SUCCESS
};

struct CanvasCache
{
    std::mutex                         mutex;
    std::condition_variable            changed; // a worker stored a reply
    std::map<std::string, CanvasEntry> entries;
};

struct CanvasReply
{
    PrinterNetworkErrorCode code;
    PrinterMmsGroup         group;
    int                     age_s; // seconds since the printer sent it
};

} // namespace

// Created once and never freed: a worker still waiting for a slow printer when the app
// exits must not find it destroyed.
static CanvasCache &canvas_cache()
{
    static CanvasCache *cache = new CanvasCache();
    return *cache;
}

// The CANVAS structs have no default values, and the printer layer never fills some of
// their fields (unit temperature and humidity, the trays' bed temperatures and serial
// number, the external spool vtTray), nor the group's flags when the printer sent no
// unit data. Zero those as soon as a reply arrives, so nothing later can read garbage.
static void clear_unset_canvas_fields(PrinterMmsGroup &group)
{
    if (group.mmsList.empty()) {
        group.connected  = false;
        group.connectNum = 0;
        group.autoRefill = false;
    }
    group.vtTray = PrinterMmsTray{};
    for (PrinterMms &unit : group.mmsList) {
        unit.temperature = 0;
        unit.humidity    = 0;
        for (PrinterMmsTray &tray : unit.trayList) {
            tray.minBedTemp   = 0;
            tray.maxBedTemp   = 0;
            tray.serialNumber = 0;
        }
    }
}

// Worker side of read_canvas: asks the printer and stores the reply (or the error) in the
// printer's entry, also when the API request has stopped waiting for it, so the next
// request can use it.
// PrinterMmsManager::getPrinterMmsInfo is the call behind the GUI's CANVAS sync dialog
// and send dialog: it asks the printer (MQTT), names the trays A1..A4, B1.. by unit
// order and matches every loaded tray to a filament preset. It would read the nozzle
// diameters from the GUI's printer preset itself; the caller read them on the GUI
// thread instead.
static void fetch_canvas(const std::string &printer_id, const std::vector<double> &nozzle_diameters)
{
    PrinterNetworkErrorCode code  = PrinterNetworkErrorCode::UNKNOWN_ERROR;
    PrinterMmsGroup         group {};
    try {
        PrinterNetworkResult<PrinterMmsGroup> reply = PrinterMmsManager::getInstance()->getPrinterMmsInfo(printer_id, &nozzle_diameters);
        code = reply.code;
        if (reply.isSuccess() && reply.hasData()) {
            group = reply.value();
            clear_unset_canvas_fields(group);
        }
    } catch (...) {
        // Keep UNKNOWN_ERROR: the entry below must be released whatever happened.
    }
    CanvasCache &cache = canvas_cache();
    {
        std::lock_guard<std::mutex> lock(cache.mutex);
        CanvasEntry &entry = cache.entries[printer_id];
        entry.fetching   = false;
        entry.has_reply  = true;
        entry.replied_at = std::chrono::steady_clock::now();
        entry.code       = code;
        entry.group      = group;
    }
    cache.changed.notify_all();
}

// The printer's CANVAS state. A reply younger than CANVAS_CACHE_S is reused, and while
// one request is asking the printer, the other requests for that printer wait for its
// answer instead of sending their own: the CC2 silently ignores requests for a while
// when it gets 3 or more in a row. Errors are kept for CANVAS_CACHE_S too, which also
// gives a printer that stopped answering a rest.
// Throws std::runtime_error("printer_timeout") when no answer comes within timeout_s.
static CanvasReply read_canvas(const std::string &printer_id, const std::vector<double> &nozzle_diameters, int timeout_s)
{
    using Clock = std::chrono::steady_clock;
    const Clock::time_point deadline = Clock::now() + std::chrono::seconds(timeout_s);
    CanvasCache &cache = canvas_cache();
    std::unique_lock<std::mutex> lock(cache.mutex);
    CanvasEntry &entry = cache.entries[printer_id]; // a std::map entry: the reference stays valid

    if (!cache.changed.wait_until(lock, deadline, [&entry] { return !entry.fetching; }))
        throw std::runtime_error("printer_timeout");
    const bool fresh = entry.has_reply && Clock::now() - entry.replied_at < std::chrono::seconds(CANVAS_CACHE_S);
    if (!fresh) {
        entry.fetching = true; // released by fetch_canvas, even after a timeout here
        lock.unlock();
        const int left_s = (int) std::chrono::duration_cast<std::chrono::seconds>(deadline - Clock::now()).count();
        run_on_worker([printer_id, nozzle_diameters] { fetch_canvas(printer_id, nozzle_diameters); }, left_s);
        lock.lock();
    }
    const int age_s = (int) std::chrono::duration_cast<std::chrono::seconds>(Clock::now() - entry.replied_at).count();
    return { entry.code, entry.group, age_s };
}

static const char *tray_status_name(TrayStatus status)
{
    switch (status) {
    case TRAY_STATUS_DISCONNECTED:              return "empty";
    case TRAY_STATUS_PRELOADED:                 return "preloaded";
    case TRAY_STATUS_LOADED:                    return "loaded";
    case TRAY_STATUS_PRELOADED_UNKNOWN_FILAMENT:
    case TRAY_STATUS_LOADED_UNKNOWN_FILAMENT:   return "unknown_filament";
    case TRAY_STATUS_ERROR:                     return "error";
    default:                                    return "unknown";
    }
}

// "#rrggbb", normalised as the CANVAS sync does it (normalize_color in PlaterExt.cpp),
// or null when the printer sent no colour.
static nlohmann::json tray_color(const std::string &color)
{
    std::string c;
    for (char ch : color)
        if (!std::isspace((unsigned char) ch))
            c += (char) std::tolower((unsigned char) ch);
    if (c.empty())
        return nullptr;
    if (c[0] != '#')
        c = "#" + c;
    return c.substr(0, 7);
}

// The CANVAS reply as JSON. Units and trays keep the printer's order; a tray's id is the
// name the GUI shows (A1..A4 for the first unit, B1.. for the second). The filament
// fields of an empty tray are null. temperature_c, humidity, bed_temp and external_spool
// are always null: the printer layer does not provide them.
static nlohmann::json canvas_json(const PrinterMmsGroup &group)
{
    nlohmann::json units  = nlohmann::json::array();
    nlohmann::json active = nullptr;
    for (const PrinterMms &unit : group.mmsList) {
        nlohmann::json trays = nlohmann::json::array();
        for (size_t i = 0; i < unit.trayList.size(); ++i) {
            const PrinterMmsTray &tray  = unit.trayList[i];
            const bool            empty = tray.status == TRAY_STATUS_DISCONNECTED;
            nlohmann::json nozzle_temp = nullptr;
            if (!empty && (tray.minNozzleTemp > 0 || tray.maxNozzleTemp > 0))
                nozzle_temp = {std::lround(tray.minNozzleTemp), std::lround(tray.maxNozzleTemp)};
            trays.push_back({
                {"id", string_or_null(tray.trayName)},
                {"unit", unit.mmsId},
                {"index", i},
                {"status", tray_status_name(tray.status)},
                {"type", empty ? nlohmann::json() : string_or_null(tray.filamentType)},
                {"name", empty ? nlohmann::json() : string_or_null(tray.filamentName)},
                {"vendor", empty ? nlohmann::json() : string_or_null(tray.vendor)},
                {"color", empty ? nlohmann::json() : tray_color(tray.filamentColor)},
                {"nozzle_temp", nozzle_temp},
                {"bed_temp", nullptr},
                // The filament preset the CANVAS dialog matched to this tray (empty when
                // the tray is not loaded or nothing matched); the sync uses it.
                {"matched_preset", string_or_null(tray.filamentPresetName)},
                {"matched_alias", string_or_null(tray.filamentPresetAlias)},
            });
            if (unit.mmsId == group.activeMmsId && tray.trayId == group.activeTrayId)
                active = {{"unit", unit.mmsId}, {"tray", string_or_null(tray.trayName)}};
        }
        units.push_back({
            {"id", unit.mmsId},
            {"name", string_or_null(unit.mmsName)},
            {"model", string_or_null(unit.mmsModel)},
            {"connected", unit.connected},
            {"temperature_c", nullptr},
            {"humidity", nullptr},
            {"trays", trays},
        });
    }
    return {
        {"connected", group.connected},
        {"auto_refill", group.autoRefill},
        {"active", active},
        {"units", units},
        {"external_spool", nullptr},
    };
}

// GET /printers/{id}/canvas
Response Controller::handle_get_canvas(const std::string &api_id)
{
    PrinterNetworkInfo p;
    if (!find_printer(api_id, p))
        return { 404, {{"error", "printer_not_found"}} };
    if (p.connectStatus != PRINTER_CONNECT_STATUS_CONNECTED)
        return { 409, {{"error", "printer_offline"}} };
    // The send dialog's own test (PrintSendDialogEx::getPrinterMmsInfo): a printer without
    // multi-filament support has no CANVAS to ask.
    if (!p.systemCapabilities.supportsMultiFilament)
        return { 409, {{"error", "canvas_unsupported"}} };

    // The tray matching only offers presets for the nozzle of the project's printer
    // preset. That preset is GUI data: read it here, on the GUI thread, and hand it over.
    nlohmann::json nozzles = run_on_ui([]() -> nlohmann::json {
        nlohmann::json out = nlohmann::json::array();
        const auto *opt = wxGetApp().preset_bundle->printers.get_edited_preset().config.option<ConfigOptionFloats>("nozzle_diameter");
        if (opt != nullptr)
            for (double d : opt->values)
                out.push_back(d);
        return out;
    });

    const CanvasReply reply = read_canvas(p.printerId, nozzles.get<std::vector<double>>(), CANVAS_TIMEOUT_S);
    if (reply.code != PrinterNetworkErrorCode::SUCCESS)
        return printer_error(reply.code);
    nlohmann::json j = canvas_json(reply.group);
    j["age_s"] = reply.age_s;
    return { 200, j };
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
            if (req.method == "GET" && action == "canvas")
                return handle_get_canvas(id);
        }
        return { 404, {{"error", "not_found"}} };
    } catch (const std::runtime_error &e) {
        if (std::string(e.what()) == "printer_timeout")
            return { 504, {{"error", "printer_timeout"}} };
        throw; // ui_timeout and the rest are answered by dispatch
    }
}

}}} // namespace Slic3r::GUI::RemoteAPI
