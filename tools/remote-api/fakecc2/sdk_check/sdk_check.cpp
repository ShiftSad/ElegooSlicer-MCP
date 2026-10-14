// End-to-end check of the fake CC2 with the real elegoo-link SDK the slicer links
// (ElegooSlicer\deps\build\ElegooSlicer_dep\usr\local\lib\elegoolink.lib, commit 46c7b81),
// called the way ElegooSlicer's wrapper (src/slic3r/Utils/Elegoo/ElegooLink.cpp) calls it.
//
// Usage: sdk_check.exe <host> <serial number> <access code> <gcode file> <log dir> [control port]
// Build and run: py -3 -I tools\remote-api\fakecc2\sdk_check\build_and_run.py
#include <winsock2.h> // for the tiny control-endpoint client below
#include <ws2tcpip.h>

#include <elegoolink/elegoo_link.h>

#include <chrono>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using namespace elink;

namespace {

int g_failures = 0;

void check(const std::string& name, bool ok, const std::string& detail = "")
{
    std::cout << (ok ? "PASS  " : "FAIL  ") << name;
    if (!ok && !detail.empty())
        std::cout << "  -- " << detail;
    std::cout << std::endl;
    if (!ok)
        ++g_failures;
}

std::string code_text(ELINK_ERROR_CODE code, const std::string& message)
{
    return "code " + std::to_string(static_cast<int>(code)) + " (" + message + ")";
}

// Every PrinterStatusEvent the SDK raises (what ElegooSlicer turns into its printer status).
class StatusEvents
{
public:
    void add(const PrinterStatusData& status)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        events_.push_back(status);
        cv_.notify_all();
    }

    bool wait_for(const std::function<bool(const PrinterStatusData&)>& predicate, int timeout_ms)
    {
        std::unique_lock<std::mutex> lock(mutex_);
        auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (true) {
            for (; next_ < events_.size(); ++next_) {
                if (predicate(events_[next_])) {
                    ++next_;
                    return true;
                }
            }
            if (cv_.wait_until(lock, deadline) == std::cv_status::timeout)
                return false;
        }
    }

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    std::vector<PrinterStatusData> events_;
    size_t next_ = 0;
};

// Connection up/down events, in order.
class ConnectionEvents
{
public:
    void add(bool connected)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        events_.push_back(connected);
        cv_.notify_all();
    }

    // Forget what happened so far: the next wait_for only looks at newer events.
    void mark()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        next_ = events_.size();
    }

    bool wait_for(bool connected, int timeout_ms)
    {
        std::unique_lock<std::mutex> lock(mutex_);
        auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (true) {
            for (; next_ < events_.size(); ++next_) {
                if (events_[next_] == connected) {
                    ++next_;
                    return true;
                }
            }
            if (cv_.wait_until(lock, deadline) == std::cv_status::timeout)
                return false;
        }
    }

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    std::vector<bool> events_;
    size_t next_ = 0;
};

// POST a JSON body to the fake's control endpoint (http://127.0.0.1:<port>/state).
bool post_control(int port, const std::string& body)
{
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<u_short>(port));
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    bool ok = false;
    if (connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0) {
        std::string request = "POST /state HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                              "Content-Length: " + std::to_string(body.size()) + "\r\nConnection: close\r\n\r\n" + body;
        send(s, request.data(), static_cast<int>(request.size()), 0);
        char reply[256] = {};
        int n = recv(s, reply, sizeof(reply) - 1, 0);
        ok = n > 0 && std::string(reply, n).find(" 200 ") != std::string::npos;
    }
    closesocket(s);
    WSACleanup();
    return ok;
}

// ElegooLink::connectToPrinter (ElegooSlicer wrapper) builds exactly these parameters.
ConnectPrinterParams connect_params(const std::string& id, const std::string& host, const std::string& serial,
                                    const std::string& access_code)
{
    ConnectPrinterParams p;
    p.printerId = id;
    p.printerType = PrinterType::ELEGOO_FDM_CC2;
    p.name = "Fake CC2";
    p.authMode = "accessCode";
    p.host = host;
    p.model = "Elegoo Centauri Carbon 2";
    p.serialNumber = serial;
    p.brand = "Elegoo";
    p.autoReconnect = true;
    p.checkConnection = true;
    p.connectionTimeout = 5 * 1000;
    p.accessCode = access_code;
    p.networkMode = NetworkMode::LAN;
    return p;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc != 6 && argc != 7) {
        std::cerr << "usage: sdk_check <host> <serial number> <access code> <gcode file> <log dir> [control port]" << std::endl;
        return 2;
    }
    const std::string host = argv[1];
    const std::string sn = argv[2];
    const std::string code = argv[3];
    const std::string gcode = argv[4];
    const std::string log_dir = argv[5];
    const int control_port = argc == 7 ? std::stoi(argv[6]) : 18080;
    const std::string id = "fakecc2-sdk-check";

    ElegooLink::Config cfg;
    cfg.log.logLevel = 1; // debug, to the file only
    cfg.log.logEnableConsole = false;
    cfg.log.logEnableFile = true;
    cfg.log.logFileName = log_dir + "/elegoolink.log";
    auto& link = ElegooLink::getInstance();
    check("sdk: initialize (" + link.getVersion() + ")", link.initialize(cfg));

    StatusEvents events;
    ConnectionEvents connections;
    link.subscribeEvent<PrinterStatusEvent>([&](const std::shared_ptr<PrinterStatusEvent>& e) { events.add(e->status); });
    link.subscribeEvent<PrinterConnectionEvent>([&](const std::shared_ptr<PrinterConnectionEvent>& e) {
        bool up = e->connectionStatus.status == ConnectionStatus::CONNECTED;
        std::cout << "INFO  connection event: " << (up ? "connected" : "disconnected") << std::endl;
        connections.add(up);
    });

    // 1. Discovery (UDP 52700 broadcast to every adapter, the loopback one included).
    PrinterDiscoveryParams discovery;
    discovery.timeoutMs = 3000;
    auto found = link.startPrinterDiscovery(discovery);
    bool discovered = false;
    if (found.isSuccess() && found.hasData()) {
        for (const auto& printer : found.value().printers) {
            if (printer.serialNumber == sn) {
                discovered = printer.host == "127.0.0.1" && printer.printerType == PrinterType::ELEGOO_FDM_CC2 &&
                             printer.authMode == "accessCode" && printer.networkMode == NetworkMode::LAN;
                std::cout << "INFO  discovered " << printer.name << " / " << printer.model << " at " << printer.host
                          << " id " << printer.printerId << std::endl;
            }
        }
    }
    check("sdk: discovery finds the fake (CC2, 127.0.0.1, accessCode, LAN)", discovered);

    // 2. Wrong access code: through /system/info (no serial) and through MQTT (serial known).
    auto bad = link.connectPrinter(connect_params(id + "-bad1", host, "", "wrong"));
    check("sdk: wrong code without serial -> INVALID_ACCESS_CODE (/system/info 401)",
          bad.code == ELINK_ERROR_CODE::INVALID_ACCESS_CODE, code_text(bad.code, bad.message));
    bad = link.connectPrinter(connect_params(id + "-bad2", host, sn, "wrong"));
    check("sdk: wrong code with serial -> INVALID_ACCESS_CODE (MQTT CONNACK 5)",
          bad.code == ELINK_ERROR_CODE::INVALID_ACCESS_CODE, code_text(bad.code, bad.message));

    // 3. Connect like a printer added by hand (empty serial -> GET /system/info first).
    auto connected = link.connectPrinter(connect_params(id, host, "", code));
    check("sdk: connectPrinter (system/info + MQTT + registration)",
          connected.isSuccess() && connected.hasData() && connected.value().isConnected,
          code_text(connected.code, connected.message));
    if (!connected.isSuccess()) {
        link.cleanup();
        return 1;
    }

    // 4. What PrinterManager::connectToPrinter asks right after connecting.
    auto attributes = link.getPrinterAttributes({id}, 3000);
    check("sdk: getPrinterAttributes -> serial, mainboard id, firmware",
          attributes.isSuccess() && attributes.value().serialNumber == sn && attributes.value().mainboardId == sn &&
              !attributes.value().firmwareVersion.empty(),
          code_text(attributes.code, attributes.message));
    if (attributes.isSuccess()) {
        const auto& caps = attributes.value().capabilities;
        check("sdk: capabilities: multi-filament, auto refill, filament mapping, bed switching",
              caps.systemCapabilities.supportsMultiFilament && caps.printCapabilities.supportsAutoRefill &&
                  caps.printCapabilities.supportsFilamentMapping && caps.printCapabilities.supportsHeatedBedSwitching);
    }
    auto status = link.getPrinterStatus({id}, 3000);
    check("sdk: getPrinterStatus -> IDLE",
          status.isSuccess() && status.value().printerStatus.state == PrinterState::IDLE, code_text(status.code, status.message));

    // 4b. Rename (PrinterManager::updatePrinterName -> method 1043), read back through 1001.
    if (attributes.isSuccess()) {
        const std::string original_name = attributes.value().name;
        UpdatePrinterNameParams rename;
        rename.printerId = id;
        rename.printerName = "Fake CC2 renamed";
        auto renamed = link.updatePrinterName(rename);
        auto renamed_attributes = link.getPrinterAttributes({id}, 3000);
        check("sdk: updatePrinterName (1043) -> success, getPrinterAttributes reports the new name",
              renamed.isSuccess() && renamed_attributes.isSuccess() && renamed_attributes.value().name == "Fake CC2 renamed",
              code_text(renamed.code, renamed.message));
        rename.printerName = original_name;
        link.updatePrinterName(rename);
    }

    // 5. CANVAS (PrinterManager::getPrinterMmsInfo -> getCanvasStatus) and auto refill.
    auto canvas = link.getCanvasStatus({id});
    bool canvas_ok = canvas.isSuccess() && canvas.value().canvases.size() == 1 &&
                     canvas.value().canvases[0].connected && canvas.value().canvases[0].trays.size() == 4 &&
                     canvas.value().canvases[0].trays[0].filamentType == "PLA" &&
                     canvas.value().canvases[0].trays[3].status == 0;
    check("sdk: getCanvasStatus -> 1 CANVAS, 4 trays (PLA ... empty)", canvas_ok, code_text(canvas.code, canvas.message));
    SetAutoRefillParams refill;
    refill.printerId = id;
    refill.enable = true;
    auto refilled = link.setAutoRefill(refill);
    canvas = link.getCanvasStatus({id});
    check("sdk: setAutoRefill(true) -> canvas autoRefill true",
          refilled.isSuccess() && canvas.isSuccess() && canvas.value().autoRefill, code_text(refilled.code, refilled.message));

    // 6. Upload (ElegooLink::sendPrintFile) with progress callbacks.
    FileUploadParams upload;
    upload.printerId = id;
    upload.storageLocation = "local";
    upload.localFilePath = gcode;
    upload.fileName = "sdk check.gcode";
    int progress_calls = 0;
    int last_percent = 0;
    auto uploaded = link.uploadFile(upload, [&](const FileUploadProgressData& p) {
        ++progress_calls;
        last_percent = p.percentage;
        return true;
    });
    check("sdk: uploadFile in 1 MiB chunks -> success, progress reaches 100%",
          uploaded.isSuccess() && progress_calls >= 2 && last_percent == 100,
          code_text(uploaded.code, uploaded.message) + " calls " + std::to_string(progress_calls));

    // 7. Start print (ElegooLink::sendPrintTask builds these params).
    StartPrintParams start;
    start.printerId = id;
    start.storageLocation = "local";
    start.fileName = upload.fileName;
    start.autoBedLeveling = true;
    start.heatedBedType = 0;
    start.enableTimeLapse = false;
    start.slotMap.push_back({0, 0, 0});
    auto started = link.startPrint(start);
    check("sdk: startPrint -> success", started.isSuccess(), code_text(started.code, started.message));
    check("sdk: status events reach PRINTING / P_PRINTING with progress > 0", events.wait_for([](const PrinterStatusData& s) {
        return s.printerStatus.state == PrinterState::PRINTING && s.printerStatus.subState == PrinterSubState::P_PRINTING &&
               s.printerStatus.progress > 0;
    }, 20000));
    status = link.getPrinterStatus({id}, 3000);
    check("sdk: print status has the file name, layers and times",
          status.isSuccess() && status.value().printStatus.fileName == upload.fileName &&
              status.value().printStatus.totalLayer > 0 && status.value().printStatus.estimatedTime > 0);
    // The test G-code says "total layer number: 30" and "estimated printing time (normal mode) = 25m 0s".
    check("sdk: totalLayer 30, currentTime + estimatedTime == 1500 s (the G-code's estimate)",
          status.isSuccess() && status.value().printStatus.totalLayer == 30 &&
              status.value().printStatus.currentTime + status.value().printStatus.estimatedTime == 1500,
          status.isSuccess() ? std::to_string(status.value().printStatus.currentTime) + " + " +
                                   std::to_string(status.value().printStatus.estimatedTime)
                             : std::string());
    check("sdk: CANVAS in the status while printing: active tray 0 (slot_map), tray 0 loaded (status 2)",
          status.isSuccess() && status.value().canvasStatus.activeTrayId == 0 &&
              !status.value().canvasStatus.canvases.empty() && !status.value().canvasStatus.canvases[0].trays.empty() &&
              status.value().canvasStatus.canvases[0].trays[0].status == 2 &&
              status.value().externalDeviceStatus.canvasConnected);
    started = link.startPrint(start);
    check("sdk: startPrint while printing -> PRINTER_BUSY", started.code == ELINK_ERROR_CODE::PRINTER_BUSY,
          code_text(started.code, started.message));

    // 8. Pause, resume (not implemented for CC2 LAN in this SDK), stop.
    auto paused = link.pausePrint({id});
    check("sdk: pausePrint -> success", paused.isSuccess(), code_text(paused.code, paused.message));
    check("sdk: status events reach P_PAUSED", events.wait_for([](const PrinterStatusData& s) {
        return s.printerStatus.subState == PrinterSubState::P_PAUSED;
    }, 10000));
    auto resumed = link.resumePrint({id});
    check("sdk: resumePrint -> OPERATION_NOT_IMPLEMENTED (method 1023 is commented out in SDK 46c7b81)",
          resumed.code == ELINK_ERROR_CODE::OPERATION_NOT_IMPLEMENTED, code_text(resumed.code, resumed.message));
    auto stopped = link.stopPrint({id});
    check("sdk: stopPrint -> success", stopped.isSuccess(), code_text(stopped.code, stopped.message));
    check("sdk: status events go back to IDLE", events.wait_for([](const PrinterStatusData& s) {
        return s.printerStatus.state == PrinterState::IDLE;
    }, 15000));

    // 8b. A late answer: a real CC2 answers stop/pause only after the head has parked. The fake
    //     stops at once but answers 1022 after 17 s, past the SDK's 15 s request timeout.
    started = link.startPrint(start);
    check("sdk: startPrint again -> success", started.isSuccess(), code_text(started.code, started.message));
    check("sdk: printing again", events.wait_for([](const PrinterStatusData& s) {
        return s.printerStatus.subState == PrinterSubState::P_PRINTING;
    }, 20000));
    check("control: delay_next 1022 by 17 s", post_control(control_port, "{\"delay_next\": {\"method\": 1022, \"seconds\": 17}}"));
    auto late_stop = link.stopPrint({id});
    check("sdk: stopPrint answered after 17 s -> OPERATION_TIMEOUT (the request timed out)",
          late_stop.code == ELINK_ERROR_CODE::OPERATION_TIMEOUT, code_text(late_stop.code, late_stop.message));
    check("sdk: ... yet the printer did stop: status events reached IDLE", events.wait_for([](const PrinterStatusData& s) {
        return s.printerStatus.state == PrinterState::IDLE;
    }, 10000));
    std::this_thread::sleep_for(std::chrono::seconds(3)); // let the late answer arrive (the SDK logs and drops it)
    status = link.getPrinterStatus({id}, 3000);
    check("sdk: still answering after the late reply", status.isSuccess() && status.value().printerStatus.state == PrinterState::IDLE,
          code_text(status.code, status.message));

    refill.enable = false;
    link.setAutoRefill(refill);
    link.disconnectPrinter(id);

    // 9. Reconnect the way the app does once the serial number is stored in printer_list.json
    //    (no /system/info, straight to MQTT).
    connected = link.connectPrinter(connect_params(id + "-sn", host, sn, code));
    status = link.getPrinterStatus({id + "-sn"}, 3000);
    check("sdk: reconnect with the stored serial number -> connected, IDLE",
          connected.isSuccess() && status.isSuccess() && status.value().printerStatus.state == PrinterState::IDLE,
          code_text(connected.code, connected.message));

    // 10. The fake goes offline and comes back: the SDK reports it and reconnects by itself
    //     (ConnectionManagerBase::reconnectLoop retries every 5 s; the slicer passes autoReconnect).
    connections.mark();
    check("control: offline", post_control(control_port, "{\"offline\": true}"));
    check("sdk: fake offline -> disconnected event", connections.wait_for(false, 10000));
    check("sdk: requests fail while offline", !link.getPrinterStatus({id + "-sn"}, 2000).isSuccess());
    check("control: back online", post_control(control_port, "{\"offline\": false}"));
    check("sdk: reconnects by itself -> connected event", connections.wait_for(true, 30000));
    status = link.getPrinterStatus({id + "-sn"}, 3000);
    check("sdk: requests answered again after the reconnect",
          status.isSuccess() && status.value().printerStatus.state == PrinterState::IDLE, code_text(status.code, status.message));
    link.disconnectPrinter(id + "-sn");
    link.cleanup();
    std::cout << (g_failures ? "FAILED" : "ALL PASSED") << " (" << g_failures << " failures)" << std::endl;
    return g_failures ? 1 : 0;
}
