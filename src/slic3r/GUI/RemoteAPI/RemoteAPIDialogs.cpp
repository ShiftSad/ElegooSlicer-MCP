// src/slic3r/GUI/RemoteAPI/RemoteAPIDialogs.cpp
//
// elegoo-mcp (D8): the dialogs skipped while the Remote API drives the window. See
// RemoteAPIDialogs.hpp for why and how GUI code uses these functions.
#include "RemoteAPIController.hpp" // first: Boost.Asio must come before <windows.h>
#include "RemoteAPIDialogs.hpp"

#include "slic3r/GUI/ConfigManipulation.hpp"
#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/I18N.hpp"
#include "slic3r/GUI/Jobs/ArrangeJob.hpp"
#include "slic3r/GUI/Jobs/OrientJob.hpp"
#include "slic3r/GUI/Jobs/Worker.hpp"
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

// Set by POST /slice when it starts a slice, taken by the next slice-completed event.
static bool s_api_slice = false; // GUI thread only

void mark_api_slice(bool started) { s_api_slice = started; }

bool take_api_slice()
{
    const bool api = s_api_slice;
    s_api_slice    = false;
    return api;
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

// The Printer tab's question (TabPrinter::toggle_options, asked whenever its Extruder page
// is on screen): firmware retraction with wipe on and "retract before wipe" below 100%.
// Its No answer turns firmware retraction off again. Every extruder variant is checked:
// the slicer refuses firmware retraction with wipe on any of them anyway
// (PrintConfig.cpp, use_firmware_retraction vs wipe), and the Yes answer fixes them all.
std::vector<std::string> printer_settings_questions(const DynamicPrintConfig &printer_config)
{
    const auto *firmware = printer_config.option<ConfigOptionBool>("use_firmware_retraction");
    const auto *wipe     = printer_config.option<ConfigOptionBools>("wipe");
    const auto *before   = printer_config.option<ConfigOptionPercents>("retract_before_wipe");
    if (firmware == nullptr || !firmware->value || wipe == nullptr || before == nullptr)
        return {};
    for (size_t i = 0; i < wipe->values.size() && i < before->values.size(); ++i)
        if (wipe->values[i] && before->values[i] < 100.0)
            // The window's text (Tab.cpp), so it reads the same in every language.
            return {_u8L("The Retract before wipe option could be only 100% when using the Firmware Retraction mode.\n"
                         "\nShall I set it to 100% in order to enable Firmware Retraction?")};
    return {};
}

// --- arrange / orient jobs started by the API ---------------------------------------
// These jobs run on a worker thread and finish on the GUI thread after the request has
// returned. A failure then opened an error dialog: the job's own (ArrangeJob: "Arrange
// failed...") or PlaterWorker's "An unexpected error occurred". For a job the API
// started, ApiJob reports it as a notification instead and keeps it for
// GET /jobs/status. Jobs started in the window are untouched.

static std::string s_last_api_job_error; // GUI thread only

namespace {
class ApiJob : public Job
{
public:
    explicit ApiJob(std::unique_ptr<Job> job) : m_job(std::move(job)) {}

    void process(Ctl &ctl) override { m_job->process(ctl); }

    void finalize(bool canceled, std::exception_ptr &eptr) override
    {
        DialogLog errors; // show_error() inside the job's finalize is skipped into this log
        m_job->finalize(canceled, eptr);
        if (eptr) {
            // What PlaterWorker would show for an exception the job left unhandled.
            try {
                std::rethrow_exception(eptr);
            } catch (const std::exception &e) {
                skip_dialog(_u8L("An unexpected error occurred") + ": " + e.what(), /*error=*/true);
                eptr = nullptr;
            } catch (...) {
                // Not an std::exception: left to the worker, as for every other job.
            }
        }
        if (!errors.skipped.empty())
            s_last_api_job_error = errors.skipped.back();
    }

private:
    std::unique_ptr<Job> m_job;
};
} // namespace

// Plater::arrange / Plater::orient: an undo snapshot, then the job (callers check that
// the worker is idle first).
static void start_api_job(std::unique_ptr<Job> job, const std::string &snapshot_name)
{
    Plater *plater = wxGetApp().plater();
    s_last_api_job_error.clear();
    plater->take_snapshot(snapshot_name);
    replace_job(plater->get_ui_job_worker(), std::make_unique<ApiJob>(std::move(job)));
}

void api_arrange() { start_api_job(std::make_unique<ArrangeJob>(), _u8L("Arrange")); }
void api_orient()  { start_api_job(std::make_unique<OrientJob>(), _u8L("Orient")); }

std::string last_api_job_error() { return s_last_api_job_error; }

}}} // namespace
