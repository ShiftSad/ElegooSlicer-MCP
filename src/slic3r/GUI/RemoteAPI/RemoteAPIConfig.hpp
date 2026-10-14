// src/slic3r/GUI/RemoteAPI/RemoteAPIConfig.hpp
#ifndef slic3r_RemoteAPIConfig_hpp_
#define slic3r_RemoteAPIConfig_hpp_

#include <string>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// Persisted in AppConfig (main-thread only: load()/save() must run on the GUI thread).
struct Config
{
    bool        enabled  { false };
    int         port     { 13130 };
    bool        bind_lan { false };
    bool        notify   { true };  // toast in-app when the API changes state
    std::string token;

    static Config      load();
    void               save() const;
    // 32 hex chars from std::random_device. Not for cryptographic secrets in
    // general, but fine for a LAN API token.
    static std::string generate_token();

    // elegoo-mcp: Preferences > Remote API > "Allow sending and controlling prints"
    // (remote_api_allow_print_control). Off unless the user turned it on: without it
    // the API never sends G-code to a printer nor starts, pauses, resumes or stops a
    // print. Read on every such request, so a change applies at once. GUI thread only.
    static bool print_control_allowed();
};

}}} // namespace

#endif
