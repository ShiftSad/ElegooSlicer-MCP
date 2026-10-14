// src/slic3r/GUI/RemoteAPI/RemoteAPIElegoo.cpp
//
// elegoo-mcp: the Elegoo printers known to ElegooSlicer (Device tab) and their CANVAS
// multi-material unit:
//   GET /printers, /printers/{id}, /printers/{id}/status, /printers/{id}/tasks,
//   /printers/{id}/files, /printers/{id}/canvas, /printers/{id}/canvas/mapping (read-only)
//   POST /printers/{id}/canvas/sync (the CANVAS trays become the project's filaments)
//   POST /printers/{id}/print/prepare, /printers/{id}/print, GET/DELETE /uploads/{id},
//   POST /printers/{id}/pause, /resume, /stop (print control: off unless the user allows
//   it in Preferences, see the "print control" section)
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
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/GCode/PostProcessor.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Print.hpp"
#include "libslic3r/PrinterNetworkInfo.hpp"
#include "libslic3r/StandardColorMatcher.hpp"
#include "libslic3r/Utils.hpp"
#include "libslic3r_version.h"
#include "slic3r/GUI/Elegoo/PrintSendDialogEx.hpp"
#include "slic3r/GUI/GUI.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/NotificationManager.hpp"
#include "slic3r/GUI/PartPlate.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/PlaterExt.hpp"
#include "slic3r/Utils/ASCIIFolding.hpp"
#include "slic3r/Utils/Elegoo/MultiInstanceCoordinator.hpp"
#include "slic3r/Utils/Elegoo/PrinterManager.hpp"
#include "slic3r/Utils/Elegoo/PrinterMmsManager.hpp"
#include "slic3r/Utils/Elegoo/PrinterUploadManager.hpp"
#include "slic3r/Utils/PrintHost.hpp"
#include "elegoolink/elegoo_link.h"

#include <boost/algorithm/string.hpp>
#include <boost/filesystem.hpp>
#include <boost/log/trivial.hpp>
#include <boost/uuid/uuid.hpp>
#include <boost/uuid/uuid_generators.hpp>
#include <boost/uuid/uuid_io.hpp>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <functional>
#include <future>
#include <initializer_list>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
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

// A colour as "#rrggbb", normalised as the CANVAS sync does it (normalize_color in
// PlaterExt.cpp), or null when there is none.
static nlohmann::json hex_color(const std::string &color)
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
                {"color", empty ? nlohmann::json() : hex_color(tray.filamentColor)},
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

// The checks every CANVAS route starts with. False, with the answer in `error`, when the
// printer is unknown, not connected, or has no CANVAS to ask: the send dialog's own test
// (PrintSendDialogEx::getPrinterMmsInfo) is the printer's multi-filament support.
static bool find_canvas_printer(const std::string &api_id, PrinterNetworkInfo &p, Response &error)
{
    if (!find_printer(api_id, p)) {
        error = { 404, {{"error", "printer_not_found"}} };
        return false;
    }
    if (p.connectStatus != PRINTER_CONNECT_STATUS_CONNECTED) {
        error = { 409, {{"error", "printer_offline"}} };
        return false;
    }
    if (!p.systemCapabilities.supportsMultiFilament) {
        error = { 409, {{"error", "canvas_unsupported"}} };
        return false;
    }
    return true;
}

// The nozzle diameters of the project's printer preset: the tray matching only offers
// filament presets made for them. That preset is GUI data: call this on the GUI thread
// (run_on_ui) and hand the result to read_canvas.
static nlohmann::json nozzle_diameters_json()
{
    nlohmann::json out = nlohmann::json::array();
    const auto *opt = wxGetApp().preset_bundle->printers.get_edited_preset().config.option<ConfigOptionFloats>("nozzle_diameter");
    if (opt != nullptr)
        for (double d : opt->values)
            out.push_back(d);
    return out;
}

// GET /printers/{id}/canvas
Response Controller::handle_get_canvas(const std::string &api_id)
{
    PrinterNetworkInfo p;
    Response           error;
    if (!find_canvas_printer(api_id, p, error))
        return error;

    const nlohmann::json nozzles = run_on_ui(nozzle_diameters_json);
    const CanvasReply    reply   = read_canvas(p.printerId, nozzles.get<std::vector<double>>(), CANVAS_TIMEOUT_S);
    if (reply.code != PrinterNetworkErrorCode::SUCCESS)
        return printer_error(reply.code);
    nlohmann::json j = canvas_json(reply.group);
    j["age_s"] = reply.age_s;
    return { 200, j };
}

// --- CANVAS sync --------------------------------------------------------------------

// Whether the sync takes a tray into the project: PresetBundle::sync_ams_list's test
// ("has_filament") on the data the sync is built from, i.e. ElegooSlicer matched a filament
// preset to the tray (matched_preset in GET /canvas). The sync leaves the other trays out
// (empty, not ready, or nothing matched) and fills the slots from these, in tray order.
static bool tray_is_synced(const PrinterMmsTray &tray)
{
    return !tray.filamentId.empty() || !tray.filamentPresetName.empty() || !tray.filamentPresetAlias.empty();
}

// The trays the sync did not take as they are, in tray order: left out ("empty", or
// "not_matched" when no filament preset matches the tray's filament), or synced with a
// fallback preset ("fallback_preset", the trays the GUI lists in its warning dialog; detail
// is that dialog's line, in English).
static nlohmann::json sync_unknown_json(const PrinterMmsGroup &group, const PlaterExt::MmsFilamentSync &sync)
{
    nlohmann::json out = nlohmann::json::array();
    for (const PrinterMms &unit : group.mmsList)
        for (const PrinterMmsTray &tray : unit.trayList) {
            const bool     empty = tray.status == TRAY_STATUS_DISCONNECTED;
            nlohmann::json item  = {{"tray", string_or_null(tray.trayName)},
                                    {"type", empty ? nlohmann::json() : string_or_null(tray.filamentType)}};
            if (!tray_is_synced(tray)) {
                item["reason"] = empty ? "empty" : "not_matched";
                out.push_back(item);
                continue;
            }
            for (const PlaterExt::MmsSyncUnknownTray &unknown : sync.unknowns)
                if (unknown.tray_name == tray.trayName) {
                    item["reason"] = "fallback_preset";
                    item["detail"] = unknown.reason;
                    out.push_back(item);
                    break;
                }
        }
    return out;
}

// The project's filament slots after a sync, each with the tray it came from. The sync fills
// slot i from the i-th tray it takes (tray_is_synced). With color_only the slots stay as they
// were and only their colours change, so slots past the last such tray have no tray.
// GUI thread.
static nlohmann::json synced_slots_json(const PrinterMmsGroup &group)
{
    std::vector<std::string> trays;
    for (const PrinterMms &unit : group.mmsList)
        for (const PrinterMmsTray &tray : unit.trayList)
            if (tray_is_synced(tray))
                trays.push_back(tray.trayName);

    PresetBundle              *bundle  = wxGetApp().preset_bundle;
    const ConfigOptionStrings *colours = bundle->project_config.option<ConfigOptionStrings>("filament_colour");
    nlohmann::json             slots   = nlohmann::json::array();
    for (size_t i = 0; i < bundle->filament_presets.size(); ++i) {
        std::string type;
        Preset     *preset = bundle->filaments.find_preset(bundle->filament_presets[i]);
        if (preset != nullptr) {
            std::string displayed_type; // unused: the type the sidebar shows
            type = preset->config.get_filament_type(displayed_type);
        }
        slots.push_back({
            {"slot", i + 1},
            {"preset", bundle->filament_presets[i]},
            {"type", string_or_null(type)},
            {"color", colours != nullptr && i < colours->values.size() ? hex_color(colours->values[i]) : nlohmann::json()},
            {"tray", i < trays.size() ? nlohmann::json(trays[i]) : nlohmann::json()},
        });
    }
    return slots;
}

// POST /printers/{id}/canvas/sync {"mode"?: "filament_and_color" | "color_only",
//                                  "discard_changes"?: bool}
// The GUI's "Sync filaments with MMS" without its dialogs: the same CANVAS read, then the
// same two steps (PlaterExt::start/finish_mms_filament_sync). The project's filament slots
// become the synced trays' presets and colours (color_only: only the colours change).
// The GUI always syncs presets and colours (it ignores the sync_ams_filament_mode
// preference), so that is the default.
Response Controller::handle_canvas_sync(const std::string &api_id, const std::string &body)
{
    bool color_only      = false;
    bool discard_changes = false;
    if (body.find_first_not_of(" \t\r\n") != std::string::npos) {
        const nlohmann::json in = nlohmann::json::parse(body); // parse_error -> 400 in route_elegoo
        if (!in.is_object())
            return { 400, {{"error", "body_must_be_object"}} };
        if (in.contains("mode")) {
            if (!in["mode"].is_string())
                return { 400, {{"error", "bad_mode"}, {"detail", "mode: \"filament_and_color\" | \"color_only\""}} };
            const std::string mode = in["mode"].get<std::string>();
            if (mode != "filament_and_color" && mode != "color_only")
                return { 422, {{"error", "bad_mode"}, {"detail", "mode: \"filament_and_color\" | \"color_only\""}} };
            color_only = mode == "color_only";
        }
        if (in.contains("discard_changes")) {
            if (!in["discard_changes"].is_boolean())
                return { 400, {{"error", "bad_discard_changes"}, {"detail", "discard_changes: true | false"}} };
            discard_changes = in["discard_changes"].get<bool>();
        }
    }

    PrinterNetworkInfo p;
    Response           error;
    if (!find_canvas_printer(api_id, p, error))
        return error;
    const nlohmann::json nozzles = run_on_ui(nozzle_diameters_json);
    const CanvasReply    reply   = read_canvas(p.printerId, nozzles.get<std::vector<double>>(), CANVAS_TIMEOUT_S);
    if (reply.code != PrinterNetworkErrorCode::SUCCESS)
        return printer_error(reply.code);
    // The sync dialog shows an error instead of the trays when the CANVAS is not connected
    // (PrinterMmsSyncView::getPrinterFilamentInfo).
    if (!reply.group.connected)
        return { 409, {{"error", "canvas_disconnected"}} };

    const PrinterMmsGroup group      = reply.group;
    const std::string     printer_id = p.printerId;
    nlohmann::json r = run_on_ui([group, printer_id, color_only, discard_changes]() -> nlohmann::json {
        if (wxGetApp().plater() == nullptr)
            return {{"error", "printers_unavailable"}};
        PresetCollection &filaments = wxGetApp().preset_bundle->filaments;
        // Step 2 selects the first synced preset in the Filament tab, which opens
        // UnsavedChangesDialog when the tab's preset has unsaved edits. Refuse before
        // changing anything, unless the client asked to drop the edits.
        if (filaments.current_is_dirty() && !discard_changes)
            return {{"error", "unsaved_filament_changes"},
                    {"preset", filaments.get_edited_preset().name},
                    {"hint", "save the filament preset (POST /preset/save), or sync with \"discard_changes\": true"}};

        // The sync dialog remembers the printer as the one to preselect next time
        // (PrinterMmsSyncView::syncMmsFilament), before the sync runs.
        wxGetApp().app_config->set("recent", CONFIG_KEY_SELECTED_PRINTER_ID, printer_id);

        const PlaterExt::MmsFilamentSync sync    = PlaterExt::start_mms_filament_sync(group, color_only);
        const nlohmann::json             unknown = sync_unknown_json(group, sync);
        if (sync.count == 0) // the GUI says "no compatible filaments"; the project is unchanged
            return {{"error", "no_compatible_filaments"}, {"unknown", unknown}};

        // Drop the edits where the dialog's "Discard" would (Tab::select_preset in step 2),
        // so step 2 opens no dialog. The preset can only be dirty here with discard_changes
        // (checked above), or if step 1 itself modified it.
        if (filaments.current_is_dirty())
            filaments.discard_current_changes();
        PlaterExt::finish_mms_filament_sync(sync);
        // Every plate prints with the new filaments: none of the slice results is valid now.
        wxGetApp().plater()->get_partplate_list().invalid_all_slice_result();

        nlohmann::json slots  = synced_slots_json(group);
        int            synced = 0;
        for (const nlohmann::json &slot : slots)
            if (!slot["tray"].is_null())
                ++synced;
        return {{"synced", synced}, {"slots", slots}, {"unknown", unknown}};
    }, 30);

    if (r.contains("error")) {
        if (r["error"] == "printers_unavailable")
            return printers_unavailable();
        return { 409, r }; // unsaved_filament_changes, no_compatible_filaments
    }
    api_notify("Synced " + std::to_string(r["synced"].get<int>()) + " filament slot(s) from the CANVAS of '" +
               p.printerName + "'");
    return { 200, r };
}

// --- CANVAS tray mapping ------------------------------------------------------------

static bool is_blank(const std::string &s)
{
    return boost::algorithm::trim_copy(s).empty();
}

static std::string upper_trimmed(const std::string &s)
{
    return boost::algorithm::to_upper_copy(boost::algorithm::trim_copy(s));
}

// Whether two colours count as the same for the tray mapping: equal once both are rounded
// to the nearest colour of ElegooSlicer's standard palette, the comparison
// PrinterMmsManager::getFilamentMmsMapping makes. Colours the palette cannot read are
// compared as text.
static bool same_standard_color(const std::string &a, const std::string &b)
{
    static const StandardColorMatcher matcher;
    const std::optional<StandardColorMatcher::Color> ma = matcher.match(a);
    const std::optional<StandardColorMatcher::Color> mb = matcher.match(b);
    if (ma && mb)
        return upper_trimmed(ma->hex) == upper_trimmed(mb->hex);
    return hex_color(a) == hex_color(b);
}

// The tray of the CANVAS reply with these ids, or null.
static const PrinterMmsTray *find_tray(const PrinterMmsGroup &group, const std::string &unit_id, const std::string &tray_id)
{
    for (const PrinterMms &unit : group.mmsList)
        for (const PrinterMmsTray &tray : unit.trayList)
            if (tray.mmsId == unit_id && tray.trayId == tray_id)
                return &tray;
    return nullptr;
}

// A filament name reduced for comparison, as the Print dialog reduces it (standardizeFilamentName
// in resources/web/printer/print_send/printsend.js, same as in PrinterMmsManager.cpp): upper case,
// without the vendor's name, "GENERIC" and an "@..." suffix, dashes as spaces.
// "Elegoo PLA+ @ECC2" (vendor Elegoo) -> "PLA+".
static std::string standard_filament_name(const std::string &name, const std::string &vendor)
{
    std::string       s            = upper_trimmed(name);
    const std::string vendor_upper = upper_trimmed(vendor);
    if (!vendor_upper.empty() && s.find(vendor_upper) != std::string::npos)
        boost::algorithm::erase_all(s, vendor_upper);
    boost::algorithm::erase_all(s, "GENERIC");
    const size_t at = s.find('@');
    if (at != std::string::npos)
        s.resize(at);
    std::replace(s.begin(), s.end(), '-', ' ');
    return boost::algorithm::trim_copy(s);
}

// Whether a tray holds another filament than the slot's preset: the Print dialog's test for a tray
// picked by hand (updateFilamentMapping in printsend.js), which refuses the tray only when both the
// filament type and the filament name differ. A type alone is not enough: the CC2 may report a
// tray of "Elegoo PLA+" as type "PLA+" while the preset's type is "PLA", and the dialog prints that.
// tray_vendor is the tray's vendor from the CANVAS reply (the mapping does not copy it).
static bool tray_holds_other_filament(const PrintFilamentMmsMapping &f, const PrinterMmsTray &t, const std::string &tray_vendor)
{
    const std::string tray_type = upper_trimmed(t.filamentType);
    const std::string tray_name = standard_filament_name(t.filamentName, tray_vendor);
    const bool other_type = !tray_type.empty() && tray_type != upper_trimmed(f.filamentType);
    const bool other_name = !tray_name.empty() && tray_name != standard_filament_name(f.filamentName, f.vendor);
    return other_type && other_name;
}

// One filament of the mapping, with its problems:
// - no_tray: no tray was mapped (the Print dialog refuses to start: "some filaments not mapped");
// - type_mismatch: the tray holds another filament: both its type and its name differ from the
//   preset's (tray_holds_other_filament);
// - color_mismatch: the tray's colour is another standard colour than the slot's;
// - tray_not_ready: the tray is no longer loaded or preloaded.
static nlohmann::json mapped_filament_json(const PrintFilamentMmsMapping &f, const PrinterMmsGroup &group, bool sliced)
{
    const PrinterMmsTray &t      = f.mappedMmsFilament;
    nlohmann::json        issues = nlohmann::json::array();
    nlohmann::json        tray   = nullptr;
    // The Print dialog's test of a mapped filament (checkFilamentMapping in print_send).
    const bool mapped = !is_blank(t.trayName) && !is_blank(t.filamentName) && !is_blank(t.filamentType) &&
                        !is_blank(t.mmsId) && !is_blank(t.trayId);
    if (!mapped) {
        issues.push_back("no_tray");
    } else {
        tray = {{"unit", t.mmsId}, {"tray", t.trayName}, {"type", t.filamentType}, {"color", hex_color(t.filamentColor)}};
        const PrinterMmsTray *now = find_tray(group, t.mmsId, t.trayId);
        if (tray_holds_other_filament(f, t, now != nullptr ? now->vendor : t.vendor))
            issues.push_back("type_mismatch");
        if (!same_standard_color(t.filamentColor, f.filamentColor))
            issues.push_back("color_mismatch");
        if (now == nullptr || (now->status != TRAY_STATUS_LOADED && now->status != TRAY_STATUS_PRELOADED))
            issues.push_back("tray_not_ready");
    }
    return {
        {"slot", f.index + 1},
        {"preset", f.filamentName},
        {"type", string_or_null(f.filamentType)},
        {"color", hex_color(f.filamentColor)},
        // The Print dialog's estimate (model, support, wipe tower and flush), rounded to
        // 3 significant digits there; it needs the slice result.
        {"weight_g", sliced ? nlohmann::json(std::round(f.filamentWeight * 100.0) / 100.0) : nlohmann::json()},
        {"tray", tray},
        {"issues", issues},
    };
}

// GET /printers/{id}/canvas/mapping[?plate=i]: the CANVAS tray each filament of a plate
// (default: the current one) would print from, decided as the Print dialog decides it: its
// list of the plate's filaments (PrintSendDialogEx::getPlateFilamentList) and its tray
// matching (PrinterMmsManager::getFilamentMmsMapping: the tray picked for that filament in
// an earlier print when it is still loaded, else a ready tray of the same colour whose
// filament matches). Read-only: nothing is saved.
Response Controller::handle_canvas_mapping(const std::string &api_id, const std::string &query)
{
    int plate = -1; // -1: the current plate
    if (!query_value(query, "plate").empty() && (!read_int_param(query, "plate", plate) || plate < 0))
        return { 400, {{"error", "bad_plate_index"}} };

    PrinterNetworkInfo p;
    Response           error;
    if (!find_canvas_printer(api_id, p, error))
        return error;

    // GUI data, read on the GUI thread: the plate's filaments as the Print dialog lists them,
    // and the nozzle diameters for the CANVAS read.
    auto           filaments = std::make_shared<std::vector<PrintFilamentMmsMapping>>();
    nlohmann::json info      = run_on_ui([plate, filaments]() -> nlohmann::json {
        PartPlateList &plates = wxGetApp().plater()->get_partplate_list();
        const int      index  = plate < 0 ? plates.get_curr_plate_index() : plate;
        if (index >= plates.get_plate_count())
            return {{"error", "unknown_plate"}};
        PartPlate *pp     = plates.get_plate(index);
        const bool sliced = pp->is_slice_result_valid();
        // The Print dialog takes the filaments of the plate's slice result. Before slicing
        // there is none: take the filaments the plate's objects are set to print with.
        *filaments = PrintSendDialogEx::getPlateFilamentList(pp, sliced ? pp->get_used_filaments() : pp->get_extruders(true));
        return {{"plate", index}, {"sliced", sliced}, {"nozzles", nozzle_diameters_json()}};
    });
    if (info.contains("error"))
        return { 404, info };

    const CanvasReply reply = read_canvas(p.printerId, info["nozzles"].get<std::vector<double>>(), CANVAS_TIMEOUT_S);
    if (reply.code != PrinterNetworkErrorCode::SUCCESS)
        return printer_error(reply.code);
    // The Print dialog does not map filaments to an unconnected CANVAS (PrintSendDialogEx::getPrinterMmsInfo).
    if (!reply.group.connected || reply.group.mmsList.empty())
        return { 409, {{"error", "canvas_disconnected"}} };
    // The Print dialog's call. It reads the tray choices earlier prints saved, never writes them.
    PrinterMmsManager::getInstance()->getFilamentMmsMapping(*filaments, reply.group);

    const bool     sliced = info["sliced"].get<bool>();
    bool           ok     = true;
    nlohmann::json list   = nlohmann::json::array();
    for (const PrintFilamentMmsMapping &f : *filaments) {
        nlohmann::json item = mapped_filament_json(f, reply.group, sliced);
        ok = ok && item["issues"].empty();
        list.push_back(item);
    }
    return { 200, {{"plate", info["plate"]}, {"sliced", sliced}, {"ok", ok}, {"filaments", list}} };
}

// --- print control ------------------------------------------------------------------
//
// POST /printers/{id}/print/prepare and POST /printers/{id}/print send the plate ElegooSlicer
// sliced to a printer and start it; GET and DELETE /uploads/{id} follow and cancel that
// transfer; POST /printers/{id}/pause, /resume and /stop act on the print in progress. They
// move a real machine, so:
// - they are off unless the user turned on Preferences > Remote API > "Allow sending and
//   controlling prints" (403 print_control_disabled), read again on every request;
// - they run in the first ElegooSlicer window only: it is the one connected to the printers
//   (a second window forwards printer reads to it, but not uploads);
// - they work with LAN printers only: the SDK cannot pause or stop a cloud printer, so the API
//   neither starts nor controls prints on one (501 unsupported_on_wan);
// - a print is sent in two steps. prepare makes the checks the Print dialog makes (and a few
//   more) and answers a summary and a plan id; print sends that plan only, within
//   PLAN_EXPIRES_S, and only while the plate still holds the slice result that was checked;
// - the file sent is always the G-code ElegooSlicer sliced for the plate, the same bytes the
//   window's Print button sends: never a file or a path from the client;
// - each of them shows a notification in ElegooSlicer, whatever remote_api_notify says.
//
// The window's Print button opens the Print dialog (PrintSendDialogEx), then exports and
// queues the upload (Plater::send_gcode_legacy, BackgroundSlicingProcess::prepare_upload,
// PrintHostJobQueue::perform_job). Those steps can open message boxes, so the API does the
// same work itself; the comments name the step of the window each part copies.

static const int PLAN_EXPIRES_S    = 300;      // a prepared print must be started within this time
static const int COMMAND_TIMEOUT_S = 20;       // pause, resume, stop (the SDK waits up to 15 s itself)
static const int UPLOAD_POLL_MS    = 500;      // how often an upload's thread reads its progress
static const int CANCEL_SETTLE_S   = 60;       // how long a cancelled transfer may still turn into a print
static const int UPLOAD_KEEP_S     = 3600;     // a finished upload stays readable this long
static const int UPLOAD_MAX_S      = 6 * 3600; // an upload that reports no end by then is called failed

// The notification of a print-control request. Shown whatever remote_api_notify says: the user
// must always see that an external tool sent, started, paused, resumed or stopped a print. Each
// is a notification of its own (a CustomNotification, not the toast that gathers the other API
// messages), and a warning stays until it is closed. Plain ASCII text. Any thread.
static void print_control_notify(const std::string &text, bool warning = false)
{
    wxGetApp().CallAfter([text, warning]() {
        Plater *plater = wxGetApp().plater();
        if (plater == nullptr || plater->get_notification_manager() == nullptr)
            return;
        plater->get_notification_manager()->push_notification(
            NotificationType::CustomNotification,
            warning ? NotificationManager::NotificationLevel::WarningNotificationLevel
                    : NotificationManager::NotificationLevel::ImportantNotificationLevel,
            text);
    });
}

// The checks every print-control route starts with. False, with the answer in `refused`, when
// the preference is off (403) or this is not the first ElegooSlicer window (409). `what` names
// the request in the notification a refused action shows ("start a print"); "" for a read.
bool Controller::check_print_control(const std::string &what, Response &refused)
{
    const bool allowed = run_on_ui([]() -> nlohmann::json { return Config::print_control_allowed(); }).get<bool>();
    if (!allowed) {
        if (!what.empty())
            print_control_notify("Remote API: refused to " + what + ": print control is off "
                                 "(Preferences > Remote API > Allow sending and controlling prints)", true);
        refused = { 403, {{"error", "print_control_disabled"},
                          {"hint", "Preferences > Remote API > Allow sending and controlling prints"}} };
        return false;
    }
    // Only the first window (MultiInstanceCoordinator's master) is connected to the printers. A
    // second one forwards printer reads to it, but PrinterUploadManager does not forward uploads.
    if (!MultiInstanceCoordinator::getInstance()->isMaster()) {
        refused = { 409, {{"error", "secondary_instance"}, {"hint", "close the other ElegooSlicer windows"}} };
        return false;
    }
    return true;
}

// The answer of a print-control route for a cloud (WAN) printer. Nothing was sent.
static Response unsupported_on_wan(const std::string &hint)
{
    return { 501, {{"error", "unsupported_on_wan"}, {"hint", hint}} };
}

// Reads a request body that may be empty (= {}). False, with the answer in `error`, when it is
// not a JSON object; a JSON syntax error is thrown (route_elegoo answers 400 invalid_json).
static bool read_body_object(const std::string &body, nlohmann::json &in, Response &error)
{
    in = nlohmann::json::object();
    if (body.find_first_not_of(" \t\r\n") == std::string::npos)
        return true;
    in = nlohmann::json::parse(body);
    if (!in.is_object()) {
        error = { 400, {{"error", "body_must_be_object"}} };
        return false;
    }
    return true;
}

// Refuses a field the route does not know: on a request that starts a print, a misspelt option
// ("timelaps") must not be silently ignored.
static bool only_known_fields(const nlohmann::json &in, std::initializer_list<const char *> known, Response &error)
{
    for (auto it = in.begin(); it != in.end(); ++it) {
        bool found = false;
        for (const char *name : known)
            found = found || it.key() == name;
        if (!found) {
            error = { 400, {{"error", "unknown_field"}, {"field", it.key()}} };
            return false;
        }
    }
    return true;
}

// Reads an optional boolean field. False, with the answer in `error`, when present but not a
// boolean (400 bad_<field>).
static bool read_bool_field(const nlohmann::json &in, const std::string &key, std::optional<bool> &value, Response &error)
{
    if (!in.contains(key))
        return true;
    if (!in[key].is_boolean()) {
        error = { 400, {{"error", "bad_" + key}, {"detail", key + ": true | false"}} };
        return false;
    }
    value = in[key].get<bool>();
    return true;
}

static std::string new_id()
{
    return boost::uuids::to_string(boost::uuids::random_generator()());
}

namespace {

// A tray the client picked for a filament slot in POST .../print/prepare.
struct TrayChoice
{
    int         slot { 0 };  // 1-based filament slot
    std::string unit;        // the unit's id as in GET .../canvas ("" = not given)
    std::string tray;        // the tray's id as in GET .../canvas ("A1")
};

// A print checked by POST .../print/prepare and waiting for POST .../print. API thread only.
struct PrintPlan
{
    std::string                           api_printer_id;
    std::string                           printer_id;   // PrinterManager's id
    std::string                           printer_name;
    std::chrono::steady_clock::time_point made_at;
    int                                   plate { 0 };
    std::string                           fingerprint;  // slice_fingerprint() when it was made
    // What the upload needs, read on the GUI thread by prepare.
    std::string                           gcode_path;   // the plate's sliced G-code (ElegooSlicer's temp file)
    std::uintmax_t                        gcode_size { 0 };
    std::time_t                           gcode_written { 0 };
    std::string                           file_name;    // the file's name on the printer
    std::string                           host_name;    // the print host's name, for post-processing scripts
    DynamicPrintConfig                    print_config; // the plate's print config, for post-processing scripts
    PrinterNetworkParams                  params;       // printer, options and tray mapping (no file yet)
    std::map<std::string, std::string>    recent;       // the Print dialog's remembered choices to update
    nlohmann::json                        summary;
};

// One print sent by POST .../print: the copy of the G-code, its transfer and the start.
struct UploadJob
{
    std::mutex  mutex; // the API thread and the job's own thread both use the fields below
    std::string printer_id;    // PrinterManager's id
    std::string api_printer_id;
    std::string printer_name;
    std::string file_name;
    std::string status { "exporting" }; // exporting | uploading | success | failed | cancelled
    bool        done { false };         // the status will not change any more (see refresh_job for a cancel)
    uint64_t    uploaded { 0 };
    uint64_t    total { 0 };
    int         progress { 0 };
    std::string message { "copying the sliced G-code" };
    int         code { 0 };             // PrinterNetworkErrorCode of a failure
    std::string task_id;                // PrinterUploadManager's transfer, once it started
    bool        cancel_requested { false };
    bool        app_closing { false };  // ended because ElegooSlicer closed or rebuilt its window
    std::chrono::steady_clock::time_point cancel_requested_at;
    std::chrono::steady_clock::time_point done_at;
};

// The printers a pause, resume or stop is being sent to (send_print_command).
struct CommandGate
{
    std::mutex              mutex;
    std::condition_variable changed;
    std::set<std::string>   busy; // PrinterManager ids
};

} // namespace

// Plans by plan id. API thread only.
static std::map<std::string, std::shared_ptr<PrintPlan>> &print_plans()
{
    static std::map<std::string, std::shared_ptr<PrintPlan>> plans;
    return plans;
}

// Uploads by upload id. The map itself is used on the API thread only; each job's fields are
// shared with its own thread under job->mutex.
static std::map<std::string, std::shared_ptr<UploadJob>> &upload_jobs()
{
    static std::map<std::string, std::shared_ptr<UploadJob>> jobs;
    return jobs;
}

// Created once and never freed, like the CANVAS cache: a worker still waiting for a printer
// when the app exits must not find it destroyed.
static CommandGate &command_gate()
{
    static CommandGate *gate = new CommandGate();
    return *gate;
}

// Forgets the plans past their time and the uploads finished more than UPLOAD_KEEP_S ago.
// API thread only.
static void forget_old_plans_and_uploads()
{
    const auto now = std::chrono::steady_clock::now();
    auto &plans = print_plans();
    for (auto it = plans.begin(); it != plans.end();)
        it = now - it->second->made_at > std::chrono::seconds(PLAN_EXPIRES_S) ? plans.erase(it) : std::next(it);
    auto &jobs = upload_jobs();
    for (auto it = jobs.begin(); it != jobs.end();) {
        bool old;
        {
            std::lock_guard<std::mutex> lock(it->second->mutex);
            old = it->second->done && now - it->second->done_at > std::chrono::seconds(UPLOAD_KEEP_S);
        }
        it = old ? jobs.erase(it) : std::next(it);
    }
}

// Whether the printer can take a new print now, from ElegooSlicer's cache: connected, and idle or
// showing its last print as completed (the Print dialog's printerBusy test), and not receiving a
// print the API is still sending. False, with the answer in `error`, when not. API thread.
static bool printer_free(const PrinterNetworkInfo &p, Response &error)
{
    if (p.connectStatus != PRINTER_CONNECT_STATUS_CONNECTED) {
        error = { 409, {{"error", "printer_offline"}} };
        return false;
    }
    if (p.printerStatus != PRINTER_STATUS_IDLE && p.printerStatus != PRINTER_STATUS_PRINT_COMPLETED) {
        error = { 409, {{"error", "printer_busy"}, {"status", printer_status_name(p.printerStatus)}} };
        return false;
    }
    for (const auto &[upload_id, job] : upload_jobs()) {
        std::lock_guard<std::mutex> lock(job->mutex);
        // A cancelled upload sends nothing more (its thread may still be cleaning up).
        if (job->printer_id == p.printerId && !job->done && !job->cancel_requested) {
            error = { 409, {{"error", "upload_in_progress"}, {"upload_id", upload_id}} };
            return false;
        }
    }
    return true;
}

// --- print control: the plate (GUI thread) ------------------------------------------

// A hash of every setting the project slices with: the printer, print and filament presets, and
// the project's own settings and filament colours. GUI thread.
static size_t project_config_hash()
{
    const DynamicPrintConfig config = wxGetApp().preset_bundle->full_config();
    std::string              text;
    for (const std::string &key : config.keys())
        text += key + '=' + config.opt_serialize(key) + '\n';
    return std::hash<std::string>{}(text);
}

// What a plan is bound to: the plate, its slice result, the G-code file that slice wrote, and the
// settings. Slicing again or changing a setting changes it. "" when the plate has no slice result
// or no G-code file. GUI thread.
static std::string slice_fingerprint(PartPlate *plate, int plate_index)
{
    const GCodeProcessorResult *result = plate->get_slice_result();
    if (result == nullptr)
        return {};
    const std::string         path = plate->get_tmp_gcode_path();
    boost::system::error_code ec;
    const std::uintmax_t      size = boost::filesystem::file_size(path, ec);
    if (ec)
        return {};
    const std::time_t written = boost::filesystem::last_write_time(path, ec);
    if (ec)
        return {};
    return std::to_string(plate_index) + '|' + std::to_string(result->id) + '|' + std::to_string(size) + '|' +
           std::to_string((long long) written) + '|' + std::to_string(project_config_hash());
}

// The Print dialog's filterModelName (resources/web/printer/print_send/printsend.js): drops the
// characters Windows does not allow in a file name, appends '_' to a reserved device name, and
// drops trailing spaces and dots.
static std::string print_dialog_file_stem(const std::string &name)
{
    std::string stem;
    for (char c : name)
        if (std::string("\\/:*?\"<>|").find(c) == std::string::npos)
            stem += c;
    const std::string upper  = boost::algorithm::to_upper_copy(stem);
    const bool        device = upper == "CON" || upper == "PRN" || upper == "AUX" || upper == "NUL" ||
                               (upper.size() == 4 && (upper.compare(0, 3, "COM") == 0 || upper.compare(0, 3, "LPT") == 0) &&
                                upper[3] >= '1' && upper[3] <= '9');
    if (device)
        stem += '_';
    while (!stem.empty() && (std::isspace((unsigned char) stem.back()) || stem.back() == '.'))
        stem.pop_back();
    return stem;
}

// The name the window gives the file on the printer, built by the same steps. GUI thread. Throws
// when the filename_format template does not process (the window shows that error).
static std::string print_file_name(const Print &print)
{
    // Plater::send_gcode_legacy: the project's G-code file name from the filename_format template
    // (BackgroundSlicingProcess::output_filepath_for_project), folded to ASCII.
    const boost::filesystem::path project = into_path(wxGetApp().plater()->get_project_filename(".3mf"));
    const std::string output = project.empty() ? print.output_filepath("")
                                               : print.output_filepath(project.parent_path().string(), project.stem().string());
    const std::string file   = boost::filesystem::path(fold_utf8_to_ascii(output)).filename().string();
    // PrintSendDialogEx::init: the remembered upload folder (printhost_path) and the file name
    // without ".gcode"; the dialog's page filters that name, and onPrint adds ".gcode" back.
    std::string name = wxGetApp().app_config->get("recent", CONFIG_KEY_PATH);
    if (!name.empty() && name.back() != '/')
        name += '/';
    name += file;
    if (boost::algorithm::ends_with(name, ".gcode"))
        name.resize(name.size() - 6);
    name = print_dialog_file_stem(name);
    if (!boost::algorithm::ends_with(name, ".gcode"))
        name += ".gcode";
    // BackgroundSlicingProcess::prepare_upload: the print statistics applied to the name.
    return print.print_statistics().finalize_output_path(name);
}

// Why a sliced plate cannot be printed (PartPlate::is_slice_result_ready_for_print is false).
static const char *not_printable_reason(const GCodeProcessorResult *result)
{
    if (result == nullptr)
        return "no_slice_result";
    if (result->toolpath_outside)
        return "toolpath_outside";
    if (result->gcode_check_result.error_code != 0)
        return "gcode_check_failed";
    return "filament_not_printable";
}

// The plate part of POST .../print/prepare, on the GUI thread: the checks the window makes before
// its Print button sends, and what the upload will need, into `plan` and `filaments`.
// {"error": ...} when the plate cannot be sent, else the figures for the summary.
static nlohmann::json read_plate_for_print(int plate_index, const std::string &printer_model, PrintPlan &plan,
                                           std::vector<PrintFilamentMmsMapping> &filaments)
{
    Plater *plater = wxGetApp().plater();
    if (plater == nullptr)
        return {{"error", "printers_unavailable"}};
    // With a G-code file or an exported .gcode.3mf open, the plates hold G-code ElegooSlicer did
    // not slice from this project; the API only sends G-code it sliced.
    if (plater->only_gcode_mode() || plater->using_exported_file())
        return {{"error", "preview_only"}, {"hint", "open or create a project and slice it"}};

    PartPlateList &plates  = plater->get_partplate_list();
    const int      current = plates.get_curr_plate_index();
    const int      index   = plate_index < 0 ? current : plate_index;
    if (index >= plates.get_plate_count())
        return {{"error", "unknown_plate"}};
    // The window prints the current plate, and only the current plate's slice result follows
    // every setting change at once (another plate is checked again when it gets selected).
    if (index != current)
        return {{"error", "plate_not_current"}, {"plate", index}, {"current", current},
                {"hint", "select the plate (POST /plates/{i}/select), slice it, then prepare again"}};
    PartPlate *plate = plates.get_plate(index);
    if (!plate->is_slice_result_valid())
        return {{"error", "not_sliced"}, {"plate", index}};
    // The window's Print button needs this too (MainFrame::get_enable_print_status).
    if (!plate->is_slice_result_ready_for_print())
        return {{"error", "not_printable"}, {"plate", index}, {"reason", not_printable_reason(plate->get_slice_result())}};

    // The window opens the Print dialog for Elegoo printers only when the printer profile uploads
    // through ElegooLink (Plater::send_gcode_legacy), and sends a .gcode.3mf instead of plain
    // G-code when the profile asks for it (use_3mf; the CC2 profiles do not).
    DynamicPrintConfig printer_config = wxGetApp().preset_bundle->printers.get_edited_preset().config;
    if (!PrintHost::support_device_list_management(printer_config))
        return {{"error", "not_elegoolink_profile"},
                {"hint", "select an Elegoo printer profile whose printer is connected through ElegooLink"}};
    const ConfigOptionBool *use_3mf = printer_config.option<ConfigOptionBool>("use_3mf");
    if (use_3mf != nullptr && use_3mf->value)
        return {{"error", "upload_format_unsupported"}, {"detail", "the printer profile sends .gcode.3mf files (use_3mf)"}};
    // The Print dialog warns when the printer is another model than the project's printer
    // profile; the API refuses: the G-code is made for that model.
    const ConfigOptionString *model_opt     = printer_config.option<ConfigOptionString>("printer_model");
    const std::string         project_model = model_opt != nullptr ? model_opt->value : std::string();
    if (project_model != printer_model)
        return {{"error", "printer_model_mismatch"}, {"printer_model", printer_model}, {"project_model", project_model}};

    const Print &print = *plate->fff_print();
    plan.plate       = index;
    plan.fingerprint = slice_fingerprint(plate, index);
    if (plan.fingerprint.empty())
        return {{"error", "not_sliced"}, {"plate", index}};
    plan.gcode_path = plate->get_tmp_gcode_path();
    boost::system::error_code ec;
    plan.gcode_size    = boost::filesystem::file_size(plan.gcode_path, ec);
    plan.gcode_written = boost::filesystem::last_write_time(plan.gcode_path, ec);
    try {
        plan.file_name = print_file_name(print);
    } catch (const std::exception &e) {
        return {{"error", "filename_format_error"}, {"detail", e.what()}};
    }
    // BackgroundSlicingProcess::prepare_upload hands the print host's name to the post-processing
    // scripts (an ElegooLink profile makes an OctoPrint host object there).
    std::unique_ptr<PrintHost> host(PrintHost::get_print_host(&printer_config));
    plan.host_name    = host ? host->get_name() : "";
    plan.print_config = print.full_print_config();
    // The Print dialog's list of the plate's filaments (PrintSendDialogEx::preparePrintTask).
    filaments = PrintSendDialogEx::getPlateFilamentList(plate, plate->get_used_filaments());

    // The summary's figures, as the Print dialog shows them.
    int layers = 0;
    for (const PrintObject *object : print.objects())
        layers = std::max(layers, (int) object->layer_count());
    const GCodeProcessorResult *result = plate->get_slice_result();
    nlohmann::json time_s = nullptr;
    if (result != nullptr && !result->print_statistics.modes.empty())
        time_s = std::lround(result->print_statistics.modes.front().time);
    bool post_processing = false;
    if (const ConfigOptionStrings *scripts = plan.print_config.option<ConfigOptionStrings>("post_process"))
        for (const std::string &script : scripts->values)
            post_processing = post_processing || !is_blank(script);

    // The Print dialog's remembered choices (PrintSendDialogEx::init): the options' defaults.
    AppConfig *ac        = wxGetApp().app_config;
    auto       recent_on = [ac](const char *key) { return std::atoi(ac->get("recent", key).c_str()) != 0; };
    return {
        {"plate", index},
        {"recent_bed_b", std::atoi(ac->get("recent", CONFIG_KEY_BEDTYPE).c_str()) == (int) btPC},
        {"recent_leveling", recent_on(CONFIG_KEY_HEATEDBEDLEVELING)},
        {"recent_timelapse", recent_on(CONFIG_KEY_TIMELAPSE)},
        {"recent_auto_refill", recent_on(CONFIG_KEY_AUTO_REFILL)},
        {"nozzles", nozzle_diameters_json()},
        {"estimated_time_s", time_s},
        {"filament_g", std::round(print.print_statistics().total_weight * 100.0) / 100.0},
        {"layers", layers},
        {"post_processing", post_processing},
    };
}

// --- print control: prepare ---------------------------------------------------------

// Reads the "mapping" field of POST .../print/prepare: [{slot, unit?, tray}].
static bool read_tray_choices(const nlohmann::json &in, std::vector<TrayChoice> &choices, Response &error)
{
    const nlohmann::json bad = {{"error", "bad_mapping"},
                                {"detail", "mapping: [{\"slot\": 1, \"unit\"?: \"0\", \"tray\": \"A1\"}], slot 1-based, "
                                           "unit and tray as in GET /printers/{id}/canvas"}};
    if (!in.is_array()) {
        error = { 400, bad };
        return false;
    }
    for (const nlohmann::json &item : in) {
        if (!item.is_object() || !item.contains("slot") || !item["slot"].is_number_integer() || item["slot"].get<int>() < 1 ||
            !item.contains("tray") || !item["tray"].is_string()) {
            error = { 400, bad };
            return false;
        }
        TrayChoice choice;
        choice.slot = item["slot"].get<int>();
        choice.tray = item["tray"].get<std::string>();
        if (item.contains("unit")) {
            if (item["unit"].is_string())
                choice.unit = item["unit"].get<std::string>();
            else if (item["unit"].is_number_integer())
                choice.unit = std::to_string(item["unit"].get<int>());
            else {
                error = { 400, bad };
                return false;
            }
        }
        choices.push_back(choice);
    }
    return true;
}

// The tray of the CANVAS reply with this id ("A1"), or null.
static const PrinterMmsTray *find_tray_by_name(const PrinterMmsGroup &group, const std::string &name)
{
    for (const PrinterMms &unit : group.mmsList)
        for (const PrinterMmsTray &tray : unit.trayList)
            if (tray.trayName == name)
                return &tray;
    return nullptr;
}

// An option of the Print dialog (leveling, timelapse, auto refill, bed side B): the client's choice,
// else the dialog's last one; off when the printer does not have it. False, with the answer in
// `error`, when the client turns on an option the printer does not have.
static bool choose_option(const char *name, const std::optional<bool> &asked, bool recent, bool supported, bool &value,
                          Response &error)
{
    if (asked.has_value() && *asked && !supported) {
        error = { 422, {{"error", "unsupported_option"}, {"option", name}} };
        return false;
    }
    value = supported && asked.value_or(recent);
    return true;
}

// POST /printers/{id}/print/prepare {"plate"?, "bed_side"?: "A"|"B", "leveling"?, "timelapse"?,
//                                    "auto_refill"?, "mapping"?: [{slot, unit?, tray}]}
// Checks a print of the current plate on this printer and answers what it would do, with a plan
// id for POST .../print. Nothing is sent to the printer (the CANVAS is read, as GET .../canvas).
Response Controller::handle_print_prepare(const std::string &api_id, const std::string &body)
{
    Response error;
    if (!check_print_control("prepare a print", error))
        return error;

    nlohmann::json in;
    if (!read_body_object(body, in, error) ||
        !only_known_fields(in, {"plate", "bed_side", "leveling", "timelapse", "auto_refill", "mapping"}, error))
        return error;
    int plate_index = -1; // -1: the current plate
    if (in.contains("plate")) {
        if (!in["plate"].is_number_integer() || in["plate"].get<int>() < 0)
            return { 400, {{"error", "bad_plate_index"}} };
        plate_index = in["plate"].get<int>();
    }
    std::optional<bool> bed_b;
    if (in.contains("bed_side")) {
        if (!in["bed_side"].is_string() || (in["bed_side"] != "A" && in["bed_side"] != "B"))
            return { 400, {{"error", "bad_bed_side"}, {"detail", "bed_side: \"A\" | \"B\""}} };
        bed_b = in["bed_side"] == "B";
    }
    std::optional<bool> leveling_asked, timelapse_asked, refill_asked;
    if (!read_bool_field(in, "leveling", leveling_asked, error) || !read_bool_field(in, "timelapse", timelapse_asked, error) ||
        !read_bool_field(in, "auto_refill", refill_asked, error))
        return error;
    std::vector<TrayChoice> choices;
    if (in.contains("mapping") && !read_tray_choices(in["mapping"], choices, error))
        return error;

    // The printer, from ElegooSlicer's cache.
    PrinterNetworkInfo p;
    if (!find_printer(api_id, p))
        return { 404, {{"error", "printer_not_found"}} };
    // The SDK sends pause, resume and stop to LAN printers only (see handle_print_command), so
    // the API does not start a print on a cloud printer: it could not stop it.
    if (p.networkType == NETWORK_TYPE_WAN)
        return unsupported_on_wan("the API cannot pause or stop a cloud printer, so it does not start prints on one: "
                                  "add the printer on the LAN, or print from ElegooSlicer's Print dialog");
    if (!printer_free(p, error))
        return error;

    // The plate and the project, on the GUI thread.
    auto           plan      = std::make_shared<PrintPlan>();
    auto           filaments = std::make_shared<std::vector<PrintFilamentMmsMapping>>();
    const std::string printer_model = p.printerModel;
    nlohmann::json info = run_on_ui([plate_index, printer_model, plan, filaments]() -> nlohmann::json {
        return read_plate_for_print(plate_index, printer_model, *plan, *filaments);
    });
    if (info.contains("error")) {
        const std::string e = info["error"].get<std::string>();
        if (e == "printers_unavailable")
            return printers_unavailable();
        if (e == "unknown_plate")
            return { 404, info };
        if (e == "filename_format_error")
            return { 422, info };
        return { 409, info };
    }

    // The CANVAS, read as GET .../canvas does (a reply is reused for 5 s). The Print dialog maps
    // the filaments to trays when the printer has a connected CANVAS unit (its mHasMms), and
    // refuses to send when that read fails.
    const bool      has_canvas = p.systemCapabilities.supportsMultiFilament;
    bool            has_mms    = false;
    PrinterMmsGroup group {};
    if (has_canvas) {
        const CanvasReply reply = read_canvas(p.printerId, info["nozzles"].get<std::vector<double>>(), CANVAS_TIMEOUT_S);
        if (reply.code != PrinterNetworkErrorCode::SUCCESS)
            return printer_error(reply.code);
        group   = reply.group;
        has_mms = group.connected && !group.mmsList.empty();
    }
    // Without a CANVAS the printer prints with the filament in its extruder: the Print dialog then
    // sends several filaments without a mapping; the API refuses.
    if (!has_mms && filaments->size() > 1)
        return { 409, {{"error", has_canvas ? "canvas_disconnected" : "canvas_unsupported"},
                       {"filaments", filaments->size()},
                       {"hint", "the plate prints with several filaments; they need a connected CANVAS"}} };
    if (!has_mms && !choices.empty())
        return { 422, {{"error", "bad_mapping"}, {"detail", "the printer has no connected CANVAS to map filaments to"}} };

    // The options. Time-lapse is offered on LAN printers only (the only ones that get here),
    // auto refill with a CANVAS only.
    bool leveling = false, timelapse = false, auto_refill = false, side_b = false;
    const bool can_switch_bed = p.printCapabilities.supportsHeatedBedSwitching;
    if (!choose_option("leveling", leveling_asked, info["recent_leveling"].get<bool>(),
                       p.printCapabilities.supportsAutoBedLeveling, leveling, error) ||
        !choose_option("timelapse", timelapse_asked, info["recent_timelapse"].get<bool>(),
                       p.printCapabilities.supportsTimeLapse && p.networkType == NETWORK_TYPE_LAN, timelapse, error) ||
        !choose_option("auto_refill", refill_asked, info["recent_auto_refill"].get<bool>(),
                       p.printCapabilities.supportsAutoRefill && has_mms, auto_refill, error) ||
        !choose_option("bed_side", bed_b, info["recent_bed_b"].get<bool>(), can_switch_bed, side_b, error))
        return error;

    // The tray of each filament.
    nlohmann::json warnings = nlohmann::json::array();
    nlohmann::json mapping  = nlohmann::json::array();
    if (has_mms) {
        // The Print dialog's own choice (it reads the choices earlier prints saved; the API saves none).
        PrinterMmsManager::getInstance()->getFilamentMmsMapping(*filaments, group);
        // The client's choices replace it, slot by slot.
        for (const TrayChoice &choice : choices) {
            PrintFilamentMmsMapping *filament = nullptr;
            for (PrintFilamentMmsMapping &f : *filaments)
                if (f.index + 1 == choice.slot)
                    filament = &f;
            if (filament == nullptr)
                return { 422, {{"error", "bad_mapping"}, {"detail", "slot " + std::to_string(choice.slot) + " is not printed by this plate"}} };
            const PrinterMmsTray *tray = find_tray_by_name(group, choice.tray);
            if (tray == nullptr || (!choice.unit.empty() && choice.unit != tray->mmsId))
                return { 422, {{"error", "bad_mapping"}, {"detail", "no tray " + choice.tray + " in the CANVAS"}} };
            // The Print dialog offers only the trays that are loaded or preloaded.
            if (tray->status != TRAY_STATUS_LOADED && tray->status != TRAY_STATUS_PRELOADED)
                return { 422, {{"error", "bad_mapping"}, {"detail", "tray " + choice.tray + " is not loaded"}} };
            filament->mappedMmsFilament = *tray;
        }
        nlohmann::json problems = nlohmann::json::array();
        for (PrintFilamentMmsMapping &f : *filaments) {
            // What the Print dialog sends for each filament (PrintSendDialogEx::onPrint): the tray's
            // ids, name, filament and colour, everything else empty.
            PrinterMmsTray sent {};
            sent.trayName      = f.mappedMmsFilament.trayName;
            sent.mmsId         = f.mappedMmsFilament.mmsId;
            sent.trayId        = f.mappedMmsFilament.trayId;
            sent.filamentColor = f.mappedMmsFilament.filamentColor;
            sent.filamentName  = f.mappedMmsFilament.filamentName;
            sent.filamentType  = f.mappedMmsFilament.filamentType;
            f.mappedMmsFilament = sent;
            // A filament without a tray, on a tray that is not ready, or on a tray holding another
            // filament (type and name differ, as the Print dialog tests it) is refused; another
            // colour only warned about.
            nlohmann::json item = mapped_filament_json(f, group, /*sliced=*/true);
            // The Print dialog also refuses a tray that reports no colour (PrintSendDialogEx::onPrint).
            const nlohmann::json &issues = item["issues"];
            if (!item["tray"].is_null() && is_blank(sent.filamentColor) &&
                std::find(issues.begin(), issues.end(), "tray_not_ready") == issues.end())
                item["issues"].push_back("tray_not_ready");
            nlohmann::json blocking = nlohmann::json::array();
            for (const nlohmann::json &issue : item["issues"]) {
                if (issue == "color_mismatch")
                    warnings.push_back({{"code", "color_mismatch"}, {"slot", f.index + 1},
                                        {"message", "slot " + std::to_string(f.index + 1) + " prints from tray " +
                                                        f.mappedMmsFilament.trayName + ", which holds another colour"}});
                else
                    blocking.push_back(issue);
            }
            if (!blocking.empty())
                problems.push_back({{"slot", f.index + 1}, {"issues", blocking}});
            mapping.push_back(item);
        }
        if (!problems.empty())
            return { 422, {{"error", "mapping_incomplete"}, {"issues", problems}, {"filaments", mapping},
                           {"hint", "load the filaments, sync the CANVAS (POST /printers/{id}/canvas/sync) "
                                    "or pick trays with \"mapping\""}} };
    } else if (has_canvas) {
        warnings.push_back({{"code", "no_canvas"}, {"message", "no CANVAS unit is connected: the printer prints with "
                                                                "the filament loaded in its extruder"}});
    }
    if (p.printerStatus == PRINTER_STATUS_PRINT_COMPLETED)
        warnings.push_back({{"code", "print_completed"}, {"message", "the printer shows its last print as completed: "
                                                                       "make sure the bed is clear"}});
    if (info["post_processing"].get<bool>())
        warnings.push_back({{"code", "post_processing"}, {"message", "the post-processing scripts of the print profile "
                                                                       "run on the file before it is sent and may rename it"}});

    // The upload's parameters, as PrintHostJobQueue::perform_job builds them from the Print
    // dialog's choices (PrintSendDialogEx::getExtendedInfo).
    plan->api_printer_id             = api_id;
    plan->printer_id                 = p.printerId;
    plan->printer_name               = p.printerName;
    plan->made_at                    = std::chrono::steady_clock::now();
    plan->params.printerId           = p.printerId;
    plan->params.bedType             = side_b ? 1 : 0; // the SDK's side B is bedType 1 (btPC in the dialog)
    plan->params.timeLapse           = timelapse;
    plan->params.heatedBedLeveling   = leveling;
    plan->params.autoRefill          = auto_refill;
    plan->params.hasMms              = has_mms;
    plan->params.uploadAndStartPrint = true;
    if (has_mms)
        plan->params.filamentMmsMappingList = *filaments;
    // The Print dialog remembers the printer and its options for next time
    // (PrintSendDialogEx::EndModal); the options the printer does not have keep their value.
    plan->recent[CONFIG_KEY_SELECTED_PRINTER_ID] = p.printerId;
    if (can_switch_bed)
        plan->recent[CONFIG_KEY_BEDTYPE] = std::to_string((int) (side_b ? btPC : btPTE));
    if (p.printCapabilities.supportsAutoBedLeveling)
        plan->recent[CONFIG_KEY_HEATEDBEDLEVELING] = leveling ? "1" : "0";
    if (p.printCapabilities.supportsTimeLapse && p.networkType == NETWORK_TYPE_LAN)
        plan->recent[CONFIG_KEY_TIMELAPSE] = timelapse ? "1" : "0";
    if (p.printCapabilities.supportsAutoRefill && has_mms)
        plan->recent[CONFIG_KEY_AUTO_REFILL] = auto_refill ? "1" : "0";
    plan->summary = {
        {"printer", p.printerName},
        {"printer_id", api_id},
        {"plate", info["plate"]},
        {"file_name", plan->file_name},
        {"estimated_time_s", info["estimated_time_s"]},
        {"filament_g", info["filament_g"]},
        {"layers", info["layers"]},
        {"bed_side", can_switch_bed ? nlohmann::json(side_b ? "B" : "A") : nlohmann::json()},
        {"leveling", leveling},
        {"timelapse", timelapse},
        {"auto_refill", auto_refill},
        {"mapping", mapping},
        {"warnings", warnings},
    };

    forget_old_plans_and_uploads();
    const std::string plan_id = new_id();
    print_plans()[plan_id] = plan;
    print_control_notify("Remote API: prepared a print of '" + plan->file_name + "' on '" + p.printerName +
                         "'. It starts only if the tool sends the start request within 5 minutes.");
    return { 200, {{"plan_id", plan_id}, {"expires_in_s", PLAN_EXPIRES_S}, {"summary", plan->summary}}};
}

// --- print control: the upload ------------------------------------------------------

// Marks the job finished. job.mutex held.
static void finish_job(UploadJob &job, const std::string &status, const std::string &message, int code = 0)
{
    job.status  = status;
    job.message = message;
    job.code    = code;
    job.done    = true;
    job.done_at = std::chrono::steady_clock::now();
    if (status == "success")
        job.progress = 100;
}

// A failed transfer's message, in the API's own words: the upload manager's message is left out
// (it is translated, and the printer layer's part of it can quote the printer id).
static std::string upload_failure_message(const UploadTaskInfo &task)
{
    const bool        sent = task.totalBytes > 0 && task.uploadedBytes >= task.totalBytes;
    const std::string what = sent ? "the file was sent but the print did not start" : "sending the file failed";
    switch (task.code) {
    case PrinterNetworkErrorCode::PRINTER_BUSY:                      return what + ": the printer is busy";
    case PrinterNetworkErrorCode::PRINTER_OFFLINE:
    case PrinterNetworkErrorCode::PRINTER_CONNECTION_ERROR:          return what + ": the printer is offline";
    case PrinterNetworkErrorCode::OPERATION_TIMEOUT:                 return what + ": the printer did not answer in time";
    case PrinterNetworkErrorCode::FILE_TOO_LARGE:                    return what + ": the file is too large for a cloud upload";
    case PrinterNetworkErrorCode::PRINTER_MISSING_BED_LEVELING_DATA: return what + ": the printer has no bed leveling data";
    case PrinterNetworkErrorCode::PRINTER_FILAMENT_RUNOUT:           return what + ": the printer reports a filament runout";
    case PrinterNetworkErrorCode::PRINTER_PRINT_FILE_NOT_FOUND:      return what + ": the printer did not find the file";
    default:                                                         return what + " (printer layer code " + std::to_string((int) task.code) + ")";
    }
}

// Reads the upload manager's state of the job's transfer into the job. job.mutex held; called by
// the job's thread and by DELETE /uploads/{id}. The manager forgets a finished transfer the first
// time it is read (PrinterUploadManager::getUploadTask), so every read must land in the job.
static void refresh_job(UploadJob &job)
{
    if (job.done || job.task_id.empty())
        return;
    const PrinterNetworkResult<UploadTaskInfo> read = PrinterUploadManager::getInstance()->getUploadTask(job.task_id);
    if (!read.isSuccess() || !read.hasData()) {
        // The manager forgets a transfer unread only when it closes: ElegooSlicer is closing or
        // rebuilding its window (a language change), which cancels every transfer.
        finish_job(job, "cancelled", "ElegooSlicer stopped the transfer: it was closing or rebuilding its window");
        job.app_closing = true;
        return;
    }
    const UploadTaskInfo &task = read.value();
    job.uploaded = task.uploadedBytes;
    job.total    = task.totalBytes;
    job.progress = task.progress;
    switch (task.status) {
    case UploadTaskStatus::UPLOADING:
        break;
    case UploadTaskStatus::SUCCESS:
        finish_job(job, "success", "the file was sent and the print started");
        break;
    case UploadTaskStatus::FAILED:
        finish_job(job, "failed", upload_failure_message(task), (int) task.code);
        break;
    case UploadTaskStatus::CANCELLED:
        if (!job.cancel_requested) {
            // Not cancelled by the API: the manager cancels every transfer when it closes (see above).
            finish_job(job, "cancelled", "ElegooSlicer stopped the transfer: it was closing or rebuilding its window");
            job.app_closing = true;
        } else if (std::chrono::steady_clock::now() - job.cancel_requested_at > std::chrono::seconds(CANCEL_SETTLE_S)) {
            // Cancelled by DELETE /uploads/{id}. The manager reports that at once, but its transfer
            // stops only at its next progress report, and a transfer that had just finished still
            // starts the print (the manager then reports success). So the job waited for that.
            finish_job(job, "cancelled", "cancelled");
        }
        break;
    }
}

// Deletes the job's copy of the G-code. The transfer may keep the file open for a moment after it
// stopped, and Windows does not delete an open file, so this tries for a few seconds.
static void remove_upload_copy(const boost::filesystem::path &path)
{
    for (int attempt = 0; attempt < 20; ++attempt) {
        boost::system::error_code ec;
        boost::filesystem::remove(path, ec);
        if (!ec)
            return;
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    BOOST_LOG_TRIVIAL(warning) << "remote-api: could not delete the upload copy " << path.string();
}

// The upload of one plan, on a thread of its own (POST .../print answers at once): the work
// BackgroundSlicingProcess::prepare_upload and PrintHostJobQueue::perform_job do for the window's
// Print button, then PrinterUploadManager's transfer, followed until it ends.
static void run_upload(std::shared_ptr<UploadJob> job, std::shared_ptr<const PrintPlan> plan)
{
    namespace fs = boost::filesystem;
    // Marks the job failed. The steps below go on only while the job is not done.
    auto fail = [&job](const std::string &message) {
        std::lock_guard<std::mutex> lock(job->mutex);
        if (!job->done)
            finish_job(*job, "failed", message);
    };
    auto is_done = [&job] {
        std::lock_guard<std::mutex> lock(job->mutex);
        return job->done;
    };
    fs::path    copy;
    std::string name = plan->file_name;
    try {
        // 1. A copy of the plate's sliced G-code under a new temp name, as prepare_upload makes it.
        //    The G-code already holds its thumbnails (written when the plate was sliced).
        copy = fs::temp_directory_path() / fs::unique_path("." SLIC3R_APP_KEY ".upload.%%%%-%%%%-%%%%-%%%%");
        std::string error;
        bool        copied = copy_file(plan->gcode_path, copy.string(), error) == CopyFileResult::SUCCESS;
        // The plan was checked against this file just before; if the plate was sliced again while
        // it was being copied, the copy is not the G-code that was confirmed.
        boost::system::error_code ec;
        copied = copied && fs::file_size(plan->gcode_path, ec) == plan->gcode_size && !ec &&
                 fs::last_write_time(plan->gcode_path, ec) == plan->gcode_written && !ec &&
                 fs::file_size(copy, ec) == plan->gcode_size && !ec;
        if (!copied)
            fail("could not copy the sliced G-code (was the plate sliced again?); prepare again");

        // 2. The print profile's post-processing scripts, run on the copy as prepare_upload runs them
        //    for a printer that is not a Bambu Lab one. They may rename the file.
        std::string source = copy.string();
        if (!is_done()) {
            try {
                run_post_process_scripts(source, false, plan->host_name, name, plan->print_config);
            } catch (const std::exception &e) {
                fail(std::string("a post-processing script failed: ") + e.what());
            }
        }

        // 3. The transfer and the start, as perform_job asks PrinterUploadManager for them. Under the
        //    lock, so a DELETE /uploads/{id} sees either no transfer yet or its task id.
        {
            std::lock_guard<std::mutex> lock(job->mutex);
            job->file_name = name;
            if (job->done) {
                // failed above
            } else if (job->cancel_requested) {
                finish_job(*job, "cancelled", "cancelled before the transfer started");
            } else {
                PrinterNetworkParams params = plan->params;
                params.filePath             = source;
                params.fileName             = name;
                const PrinterNetworkResult<std::string> started = PrinterUploadManager::getInstance()->startAsyncUpload(params);
                if (!started.isSuccess() || !started.hasData()) {
                    finish_job(*job, "failed", "the transfer could not start (printer layer code " +
                                                   std::to_string((int) started.code) + ")", (int) started.code);
                } else {
                    job->task_id = started.value();
                    job->status  = "uploading";
                    job->message = "sending the file to the printer";
                }
            }
        }

        // 4. Follow the transfer until it ends.
        const auto started_at = std::chrono::steady_clock::now();
        while (!is_done()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(UPLOAD_POLL_MS));
            std::lock_guard<std::mutex> lock(job->mutex);
            refresh_job(*job);
            if (!job->done && std::chrono::steady_clock::now() - started_at > std::chrono::seconds(UPLOAD_MAX_S))
                finish_job(*job, "failed", "the transfer reported no end");
        }
    } catch (const std::exception &e) {
        fail(std::string("the upload stopped: ") + e.what());
    }
    if (!copy.empty())
        remove_upload_copy(copy);

    // 5. Tell the user how it ended. Not when ElegooSlicer stopped it (its window is going away),
    //    and not for a cancel: DELETE /uploads/{id} told it already.
    std::string status, message;
    bool        closing = false;
    {
        std::lock_guard<std::mutex> lock(job->mutex);
        status  = job->status;
        message = job->message;
        name    = job->file_name; // the name post-processing may have changed
        closing = job->app_closing;
    }
    if (closing)
        return;
    if (status == "success")
        print_control_notify("Remote API: '" + name + "' was sent to '" + plan->printer_name + "' and is printing");
    else if (status == "failed")
        print_control_notify("Remote API: printing '" + name + "' on '" + plan->printer_name + "' failed: " + message, true);
}

// POST /printers/{id}/print {"plan_id"}: sends the plan POST .../print/prepare made and starts the
// print. Answers 202 with the upload id at once; GET /uploads/{id} follows the transfer.
Response Controller::handle_print_start(const std::string &api_id, const std::string &body)
{
    Response error;
    if (!check_print_control("start a print", error))
        return error;
    nlohmann::json in;
    if (!read_body_object(body, in, error) || !only_known_fields(in, {"plan_id"}, error))
        return error;
    if (!in.contains("plan_id") || !in["plan_id"].is_string())
        return { 400, {{"error", "bad_plan_id"}, {"detail", "plan_id: the id POST /printers/{id}/print/prepare answered"}} };
    const std::string plan_id = in["plan_id"].get<std::string>();

    auto      &plans = print_plans();
    const auto found = plans.find(plan_id);
    if (found == plans.end() || found->second->api_printer_id != api_id)
        return { 404, {{"error", "unknown_plan"}} };
    const std::shared_ptr<PrintPlan> plan = found->second;
    if (std::chrono::steady_clock::now() - plan->made_at > std::chrono::seconds(PLAN_EXPIRES_S)) {
        plans.erase(found);
        return { 410, {{"error", "plan_expired"}, {"detail", "the plan is older than 5 minutes; prepare again"}} };
    }

    // The printer again: it may have started something since.
    PrinterNetworkInfo p;
    if (!find_printer(api_id, p))
        return { 404, {{"error", "printer_not_found"}} };
    if (!printer_free(p, error))
        return error;

    // The plate again, on the GUI thread: still the current one, holding the slice result that was
    // checked, with the same settings. Then the choices are remembered, as the Print dialog does
    // when it sends (PrintSendDialogEx::EndModal).
    nlohmann::json check = run_on_ui([plan]() -> nlohmann::json {
        Plater *plater = wxGetApp().plater();
        if (plater == nullptr)
            return {{"error", "printers_unavailable"}};
        PartPlateList &plates = plater->get_partplate_list();
        PartPlate     *plate  = plates.get_curr_plate();
        if (plater->only_gcode_mode() || plater->using_exported_file() || plates.get_curr_plate_index() != plan->plate ||
            !plate->is_slice_result_ready_for_print() || slice_fingerprint(plate, plan->plate) != plan->fingerprint)
            return {{"error", "plan_expired"},
                    {"detail", "the plate was sliced again or changed, another plate was selected, or a setting changed; prepare again"}};
        for (const auto &[key, value] : plan->recent)
            wxGetApp().app_config->set("recent", key, value);
        return {{"ok", true}};
    });
    if (check.contains("error")) {
        if (check["error"] == "printers_unavailable")
            return printers_unavailable();
        plans.erase(plan_id);
        return { 410, check };
    }
    plans.erase(plan_id); // a plan sends one print

    auto job            = std::make_shared<UploadJob>();
    job->printer_id     = plan->printer_id;
    job->api_printer_id = plan->api_printer_id;
    job->printer_name   = plan->printer_name;
    job->file_name      = plan->file_name;
    forget_old_plans_and_uploads();
    const std::string upload_id = new_id();
    upload_jobs()[upload_id]    = job;
    std::thread([job, plan] { run_upload(job, plan); }).detach();
    print_control_notify("Remote API: sending '" + plan->file_name + "' to '" + plan->printer_name + "' to print it");
    return { 202, {{"upload_id", upload_id}} };
}

static nlohmann::json upload_json(const UploadJob &job)
{
    nlohmann::json j = {
        {"status", job.status},
        {"progress", job.progress},
        {"bytes", {job.uploaded, job.total}},
        {"message", job.message},
        {"printer_id", job.api_printer_id},
        {"file_name", job.file_name},
    };
    if (job.status == "failed" && job.code != 0)
        j["code"] = job.code;
    return j;
}

// GET /uploads/{id}: how a print sent by POST .../print is going. The job's thread reads the
// transfer's progress every UPLOAD_POLL_MS.
Response Controller::handle_get_upload(const std::string &upload_id)
{
    Response error;
    if (!check_print_control("", error))
        return error;
    const auto found = upload_jobs().find(upload_id);
    if (found == upload_jobs().end())
        return { 404, {{"error", "unknown_upload"}} };
    std::lock_guard<std::mutex> lock(found->second->mutex);
    return { 200, upload_json(*found->second) };
}

// DELETE /uploads/{id}: stops a print sent by POST .../print before it reaches the printer. Once
// the whole file was sent the printer starts it: then 409 too_late (stop it with POST .../stop).
Response Controller::handle_cancel_upload(const std::string &upload_id)
{
    Response error;
    if (!check_print_control("cancel an upload", error))
        return error;
    const auto found = upload_jobs().find(upload_id);
    if (found == upload_jobs().end())
        return { 404, {{"error", "unknown_upload"}} };
    UploadJob                  &job = *found->second;
    std::lock_guard<std::mutex> lock(job.mutex);
    if (job.status == "cancelled")
        return { 200, {{"ok", true}} };
    refresh_job(job); // the latest progress, read now
    if (job.done || (job.total > 0 && job.uploaded >= job.total))
        return { 409, {{"error", "too_late"}, {"status", job.status},
                       {"hint", "the file was sent; stop the print with POST /printers/{id}/stop"}} };
    job.cancel_requested    = true;
    job.cancel_requested_at = std::chrono::steady_clock::now();
    job.status              = "cancelled";
    job.message             = "cancelled";
    // While the file is copied there is no transfer yet: the job's thread sees cancel_requested
    // before it starts one.
    if (!job.task_id.empty())
        PrinterUploadManager::getInstance()->cancelUploadTask(job.task_id);
    print_control_notify("Remote API: cancelled sending '" + job.file_name + "' to '" + job.printer_name + "'");
    return { 200, {{"ok", true}} };
}

// --- print control: pause, resume, stop ---------------------------------------------

// Sends pause, resume or stop to the printer, on a worker thread, one command at a time per
// printer (the CC2 ignores requests for a while when it gets several in a row). ElegooSlicer's
// printer layer has no such calls, so this calls the SDK's own singleton directly, the way that
// layer calls it (sdk: elegoo_link.h pausePrint, resumePrint, stopPrint). Throws
// std::runtime_error("printer_timeout") when no answer comes within timeout_s: the command may
// still reach the printer after that.
static PrinterNetworkErrorCode send_print_command(const std::string &printer_id, const std::string &command, int timeout_s)
{
    using Clock = std::chrono::steady_clock;
    const Clock::time_point deadline = Clock::now() + std::chrono::seconds(timeout_s);
    CommandGate            &gate     = command_gate();
    {
        std::unique_lock<std::mutex> lock(gate.mutex);
        if (!gate.changed.wait_until(lock, deadline, [&gate, &printer_id] { return gate.busy.count(printer_id) == 0; }))
            throw std::runtime_error("printer_timeout");
        gate.busy.insert(printer_id); // released by the worker below, even after a timeout here
    }
    auto      sdk_code = std::make_shared<elink::ELINK_ERROR_CODE>(elink::ELINK_ERROR_CODE::UNKNOWN_ERROR);
    const int left_s   = (int) std::chrono::duration_cast<std::chrono::seconds>(deadline - Clock::now()).count();
    run_on_worker([printer_id, command, sdk_code] {
        try {
            elink::ElegooLink       &sdk = elink::ElegooLink::getInstance();
            const elink::PrinterBaseParams params(printer_id);
            const elink::VoidResult  result = command == "pause"  ? sdk.pausePrint(params) :
                                              command == "resume" ? sdk.resumePrint(params) :
                                                                    sdk.stopPrint(params);
            *sdk_code = result.code;
        } catch (...) {
            // Keep UNKNOWN_ERROR: the gate below must be released whatever happened.
        }
        CommandGate &gate = command_gate();
        {
            std::lock_guard<std::mutex> lock(gate.mutex);
            gate.busy.erase(printer_id);
        }
        gate.changed.notify_all();
    }, left_s);
    // The SDK's error codes have the same numbers as ElegooSlicer's PrinterNetworkErrorCode
    // (elegoolink/types/base.h and libslic3r/PrinterNetworkResult.hpp; ElegooLink.cpp maps them
    // one to one).
    return (PrinterNetworkErrorCode) (int) *sdk_code;
}

// POST /printers/{id}/pause, /resume, /stop {"confirm": "stop"}: acts on the print in progress.
Response Controller::handle_print_command(const std::string &api_id, const std::string &command, const std::string &body)
{
    Response error;
    if (!check_print_control(command + " a print", error))
        return error;
    nlohmann::json in;
    if (!read_body_object(body, in, error) || !only_known_fields(in, {"confirm"}, error))
        return error;
    // A stop cannot be undone: the body must say so, so that a stray call does not end a print.
    if (command == "stop" && !(in.contains("confirm") && in["confirm"] == "stop"))
        return { 400, {{"error", "confirm_required"}, {"hint", "send {\"confirm\": \"stop\"}"}} };

    PrinterNetworkInfo p;
    if (!find_printer(api_id, p))
        return { 404, {{"error", "printer_not_found"}} };
    // The SDK's pausePrint, resumePrint and stopPrint always go to its LAN service, which does
    // not know a cloud printer (it would answer "printer not found"). Say so instead.
    if (p.networkType == NETWORK_TYPE_WAN)
        return unsupported_on_wan("the Elegoo SDK sends pause, resume and stop over the LAN only: "
                                  "use the printer's screen");
    if (p.connectStatus != PRINTER_CONNECT_STATUS_CONNECTED)
        return { 409, {{"error", "printer_offline"}} };
    // Whether the command fits the printer's state, from ElegooSlicer's cache: pause a print that
    // is printing, resume one that is paused, stop one that has not ended. The printer reports a
    // print's start (heating, leveling) under other states, so stop is refused only when idle.
    const PrinterStatus  status = p.printerStatus;
    const nlohmann::json state  = printer_status_name(status);
    if (command == "pause" && status != PRINTER_STATUS_PRINTING)
        return { 409, {{"error", "not_printing"}, {"status", state}} };
    if (command == "resume" && status != PRINTER_STATUS_PAUSED)
        return { 409, {{"error", "not_paused"}, {"status", state}} };
    if (command == "stop" && (status == PRINTER_STATUS_IDLE || status == PRINTER_STATUS_PRINT_COMPLETED ||
                              status == PRINTER_STATUS_CANCELED))
        return { 409, {{"error", "not_printing"}, {"status", state}} };

    const std::string what = (p.printTask.fileName.empty() ? std::string("the print") : "'" + p.printTask.fileName + "'") +
                             " on '" + p.printerName + "'";
    PrinterNetworkErrorCode code;
    try {
        code = send_print_command(p.printerId, command, COMMAND_TIMEOUT_S);
    } catch (const std::runtime_error &e) {
        if (std::string(e.what()) != "printer_timeout")
            throw;
        print_control_notify("Remote API: sent '" + command + "' for " + what + " but got no answer yet: check the printer", true);
        return { 504, {{"error", "printer_timeout"},
                       {"hint", "the printer may still carry out the command: check GET /printers/{id}/status"}} };
    }
    if (code == PrinterNetworkErrorCode::SUCCESS) {
        const char *done = command == "pause" ? "paused" : command == "resume" ? "resumed" : "stopped";
        print_control_notify("Remote API: " + std::string(done) + " " + what);
        return { 200, {{"ok", true}} };
    }
    print_control_notify("Remote API: could not " + command + " " + what + " (printer layer code " +
                         std::to_string((int) code) + ")", true);
    // The SDK has no resume for a CC2 on the LAN (method 1023 is not wired in SDK 46c7b81).
    if (code == PrinterNetworkErrorCode::OPERATION_NOT_IMPLEMENTED)
        return command == "resume" ? Response{ 501, {{"error", "resume_unsupported"}, {"hint", "resume on the printer's screen"}} }
                                   : Response{ 501, {{"error", "command_unsupported"}, {"command", command}} };
    if (code == PrinterNetworkErrorCode::OPERATION_TIMEOUT)
        return { 504, {{"error", "printer_timeout"},
                       {"hint", "the printer may still carry out the command: check GET /printers/{id}/status"}} };
    if (code == PrinterNetworkErrorCode::PRINTER_BUSY)
        return { 409, {{"error", "printer_busy"}, {"status", state}} };
    return printer_error(code);
}

// --- route group --------------------------------------------------------------------

// Every /api/v1/printers and /api/v1/uploads route (dispatch sends both prefixes here).
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
            if (req.method == "POST" && action == "canvas/sync")
                return handle_canvas_sync(id, req.body);
            if (req.method == "GET" && action == "canvas/mapping")
                return handle_canvas_mapping(id, query);
            if (req.method == "POST" && action == "print/prepare")
                return handle_print_prepare(id, req.body);
            if (req.method == "POST" && action == "print")
                return handle_print_start(id, req.body);
            if (req.method == "POST" && (action == "pause" || action == "resume" || action == "stop"))
                return handle_print_command(id, action, req.body);
        }
        // /api/v1/uploads/<upload id>
        static const std::string upload_pfx = "/api/v1/uploads/";
        if (path.size() > upload_pfx.size() && path.compare(0, upload_pfx.size(), upload_pfx) == 0 &&
            path.find('/', upload_pfx.size()) == std::string::npos) {
            const std::string upload_id = path.substr(upload_pfx.size());
            if (req.method == "GET")
                return handle_get_upload(upload_id);
            if (req.method == "DELETE")
                return handle_cancel_upload(upload_id);
        }
        return { 404, {{"error", "not_found"}} };
    } catch (const nlohmann::json::parse_error &) {
        return { 400, {{"error", "invalid_json"}} };
    } catch (const std::runtime_error &e) {
        if (std::string(e.what()) == "printer_timeout")
            return { 504, {{"error", "printer_timeout"}} };
        throw; // ui_timeout and the rest are answered by dispatch
    }
}

}}} // namespace Slic3r::GUI::RemoteAPI
