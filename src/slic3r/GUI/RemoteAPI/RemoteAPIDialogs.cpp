// src/slic3r/GUI/RemoteAPI/RemoteAPIDialogs.cpp
//
// elegoo-mcp (D8): the dialogs skipped while the Remote API drives the window. See
// RemoteAPIDialogs.hpp for why and how GUI code uses these functions.
#include "RemoteAPIController.hpp" // first: Boost.Asio must come before <windows.h>
#include "RemoteAPIDialogs.hpp"

#include "slic3r/GUI/ConfigManipulation.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/NotificationManager.hpp"
#include "slic3r/GUI/Plater.hpp"

#include <wx/thread.h>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// The log of the API request whose GUI work is running (GUI thread only).
static DialogLog *s_current_log = nullptr;

DialogLog::DialogLog() : outer(s_current_log) { s_current_log = this; }
DialogLog::~DialogLog() { s_current_log = outer; }

DialogLog *current_dialog_log() { return wxThread::IsMain() ? s_current_log : nullptr; }

bool api_skips_dialogs() { return current_dialog_log() != nullptr; }

// Shows what the dialog would have said, in the window's notification area, so whoever
// is at the machine still sees it. "Remote API:" says why there was no dialog.
static void notify(const std::string &text, bool error)
{
    Plater *plater = wxGetApp().plater();
    if (plater == nullptr || plater->get_notification_manager() == nullptr)
        return;
    plater->get_notification_manager()->push_notification(NotificationType::CustomNotification,
        error ? NotificationManager::NotificationLevel::ErrorNotificationLevel
              : NotificationManager::NotificationLevel::WarningNotificationLevel,
        "Remote API: " + text);
}

bool skip_dialog(const std::string &text, bool error)
{
    DialogLog *log = current_dialog_log();
    if (log == nullptr)
        return false;
    if (!log->quiet) {
        log->skipped.push_back(text);
        notify(text, error);
    }
    return true;
}

bool api_question(const std::string &text)
{
    DialogLog *log = current_dialog_log();
    if (log == nullptr) // not expected: callers check api_skips_dialogs() first
        return false;
    log->questions.push_back(text);
    if (!log->answer_yes)
        log->refused_question = text;
    if (!log->quiet) {
        const std::string reported = text + "\n(answered " + (log->answer_yes ? "Yes" : "No") + ")";
        log->skipped.push_back(reported);
        notify(reported, false);
    }
    return log->answer_yes;
}

// Runs the Print tab's own checks (TabPrint::update -> update_print_fff_config) on a copy
// of the staged print settings, in a quiet log: nothing is shown or changed, and the
// questions the checks would ask are returned (spiral vase needs one wall, alternate
// extra wall with "ensure vertical shell thickness: all", fuzzy skin needs Arachne).
std::vector<std::string> print_settings_questions(const DynamicPrintConfig &print_config)
{
    DynamicPrintConfig copy = print_config;
    ConfigManipulation checks; // no callbacks: it only edits `copy`
    DialogLog          dry_run;
    dry_run.quiet = true;
    checks.update_print_fff_config(&copy, /*is_global_config=*/true);
    return dry_run.questions;
}

}}} // namespace
