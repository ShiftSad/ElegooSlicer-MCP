// Cross-check of the fake CC2's MQTT broker with the real Paho MQTT C++ library the slicer links
// (deps/build/ElegooSlicer_dep, Paho C++ 1.5.3), driven the way elegoo-link drives it:
// MqttProtocol::Impl::doConnect (src/lan/protocols/mqtt_protocol.cpp) + ElegooCC2MqttProtocol
// (src/lan/adapters/elegoo_fdm_cc2/elegoo_fdm_cc2_protocol.cpp).
//
// Usage: paho_check.exe <host:port> <serial number> <access code>
// Build and run: py -3 -I tools\remote-api\fakecc2\paho_check\build_and_run.py
#include <mqtt/async_client.h>

#include <chrono>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <mutex>
#include <random>
#include <string>
#include <utility>
#include <vector>

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

// Collects every message, like the SDK's message_arrived callback (mqtt_protocol.cpp).
class Collector : public mqtt::callback
{
public:
    void connection_lost(const std::string& cause) override { std::cout << "INFO  connection lost: " << cause << std::endl; }

    void message_arrived(mqtt::const_message_ptr msg) override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        messages_.emplace_back(msg->get_topic(), msg->to_string());
        cv_.notify_all();
    }

    // First message (not taken yet) whose topic matches and whose payload contains every needle.
    bool wait_for(const std::string& topic, const std::vector<std::string>& needles, int timeout_ms, std::string* payload)
    {
        std::unique_lock<std::mutex> lock(mutex_);
        auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (true) {
            for (auto it = messages_.begin(); it != messages_.end(); ++it) {
                bool match = it->first == topic;
                for (const auto& needle : needles)
                    match = match && it->second.find(needle) != std::string::npos;
                if (match) {
                    if (payload)
                        *payload = it->second;
                    messages_.erase(it);
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
    std::vector<std::pair<std::string, std::string>> messages_;
};

// The options doConnect builds, plus ElegooCC2MqttProtocol::configureConnectionOptions (accessCode).
mqtt::connect_options sdk_options(const std::string& access_code)
{
    mqtt::connect_options opts;
    opts.set_keep_alive_interval(60);
    opts.set_clean_session(true);
    opts.set_automatic_reconnect(false);
    opts.set_user_name("elegoo");
    opts.set_password(access_code);
    return opts;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc != 4) {
        std::cerr << "usage: paho_check <host:port> <serial number> <access code>" << std::endl;
        return 2;
    }
    const std::string uri = std::string("tcp://") + argv[1];
    const std::string sn = argv[2];
    const std::string code = argv[3];
    std::cout << "INFO  " << mqtt::VERSION_STR << ", broker " << uri << std::endl;

    std::mt19937 gen(std::random_device{}());
    const std::string client_id = "1_PC_" + std::to_string(std::uniform_int_distribution<>(1000, 9999)(gen));
    const std::string request_id = client_id + "_req";

    // 1. Wrong access code: the SDK catches mqtt::exception and maps the return code (4/5 -> INVALID_ACCESS_CODE).
    {
        mqtt::async_client bad(uri, client_id + "x");
        int rc = 0;
        try {
            auto tok = bad.connect(sdk_options("wrong"));
            tok->wait_for(std::chrono::milliseconds(5000));
            rc = tok->get_return_code();
        } catch (const mqtt::exception& e) {
            rc = e.get_return_code();
        }
        check("paho: wrong access code is refused (return code " + std::to_string(rc) + ")", rc == 5 || rc == 4,
              std::to_string(rc));
    }

    mqtt::async_client client(uri, client_id);
    Collector collector;
    client.set_callback(collector);

    // 2. Connect + wait like doConnect (5 s).
    bool connected = false;
    try {
        auto tok = client.connect(sdk_options(code));
        connected = tok->wait_for(std::chrono::milliseconds(5000)) && client.is_connected();
    } catch (const mqtt::exception& e) {
        std::cout << "INFO  connect exception: " << e.what() << std::endl;
    }
    check("paho: connect with user 'elegoo' + access code", connected);
    if (!connected)
        return 1;

    // 3. Subscriptions (not awaited, like the SDK), then the registration publish at QoS 1.
    const std::string topic_response = "elegoo/" + sn + "/" + client_id + "/api_response";
    const std::string topic_status = "elegoo/" + sn + "/api_status";
    const std::string topic_register = "elegoo/" + sn + "/" + request_id + "/register_response";
    const std::string topic_request = "elegoo/" + sn + "/" + client_id + "/api_request";
    for (const auto& topic : {topic_response, topic_status, topic_register})
        client.subscribe(topic, 1);
    const std::string reg = "{\"client_id\":\"" + client_id + "\",\"request_id\":\"" + request_id + "\"}";
    bool sent = client.publish("elegoo/" + sn + "/api_register", reg, 1, false)->wait_for(std::chrono::seconds(2));
    check("paho: registration publish delivered (QoS 1 PUBACK within 2 s)", sent);
    check("paho: register_response {client_id, error: ok}",
          collector.wait_for(topic_register, {"\"client_id\":\"" + client_id + "\"", "\"error\":\"ok\""}, 3000, nullptr));

    // 4. Requests as ElegooFdmCC2MessageAdapter::convertRequest writes them; answers matched by id.
    auto request = [&](int id, int method, const std::string& params, const std::vector<std::string>& expect) {
        std::string body = "{\"id\":" + std::to_string(id) + ",\"method\":" + std::to_string(method) + ",\"params\":" + params + "}";
        bool ok = client.publish(topic_request, body, 1, false)->wait_for(std::chrono::seconds(2));
        std::vector<std::string> needles = {"\"id\":" + std::to_string(id) + ",", "\"method\":" + std::to_string(method)};
        needles.insert(needles.end(), expect.begin(), expect.end());
        std::string payload;
        ok = ok && collector.wait_for(topic_response, needles, 3000, &payload);
        return std::make_pair(ok, payload);
    };
    auto r = request(100000, 1002, "{}", {"\"error_code\":0", "\"machine_status\""});
    check("paho: 1002 GET_PRINTER_STATUS answered", r.first, r.second.substr(0, 200));
    r = request(100001, 1001, "{}", {"\"sn\":\"" + sn + "\"", "\"machine_model\""});
    check("paho: 1001 GET_PRINTER_ATTRIBUTES answered with the serial number", r.first, r.second.substr(0, 200));
    r = request(100002, 2005, "{}", {"\"canvas_info\"", "\"tray_list\""});
    check("paho: 2005 GET_CANVAS_STATUS answered", r.first, r.second.substr(0, 200));

    // 5. Heartbeat (ElegooCC2MqttProtocol::createHeartbeatMessage) on the command topic.
    client.publish(topic_request, "{\"type\":\"PING\"}", 1, false)->wait_for(std::chrono::seconds(2));
    check("paho: PING -> PONG", collector.wait_for(topic_response, {"\"type\":\"PONG\""}, 3000, nullptr));

    // 6. A change that the printer pushes as a 6000 delta on api_status.
    r = request(100003, 2004, "{\"auto_refill\":true}", {"\"error_code\":0"});
    check("paho: 2004 SET_AUTO_REFILL answered", r.first, r.second.substr(0, 200));
    std::string push;
    check("paho: 6000 status push with the change arrives on api_status",
          collector.wait_for(topic_status, {"\"method\":6000", "\"auto_refill\":true"}, 3000, &push), push);
    request(100004, 2004, "{\"auto_refill\":false}", {"\"error_code\":0"});

    // 7. Clean disconnect, as doDisconnect.
    bool disconnected = client.disconnect()->wait_for(std::chrono::seconds(2));
    check("paho: disconnect", disconnected);

    std::cout << (g_failures ? "FAILED" : "ALL PASSED") << " (" << g_failures << " failures)" << std::endl;
    return g_failures ? 1 : 0;
}
