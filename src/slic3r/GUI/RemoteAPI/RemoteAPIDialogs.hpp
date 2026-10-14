// src/slic3r/GUI/RemoteAPI/RemoteAPIDialogs.hpp
//
// elegoo-mcp (D8): the dialogs ElegooSlicer would open while the Remote API drives it.
//
// An API request does its GUI work on the GUI thread (Controller::run_on_ui), and nobody
// may be at the machine to answer a dialog: a modal one would block that thread, and
// with it every later API call, until someone closes it. So the GUI code an API request
// can reach asks api_skips_dialogs() before opening a dialog and, when it is true, calls
// skip_dialog() (a message box) or api_question() (a Yes/No question) instead. The same
// holds while an arrange/orient job the API started reports its result, after the request
// has returned (ApiJob in RemoteAPIDialogs.cpp).
//
// No Boost.Asio in here (unlike RemoteAPIController.hpp), so any GUI source file can
// include this header wherever it likes.
#ifndef slic3r_RemoteAPIDialogs_hpp_
#define slic3r_RemoteAPIDialogs_hpp_

#include <string>
#include <vector>

namespace Slic3r { namespace GUI { namespace RemoteAPI {

// --- For GUI code. GUI thread only (from another thread they report "no API request").

// True while no dialog may open (see above).
bool api_skips_dialogs();

// For a message box (an OK button only). When api_skips_dialogs(): shows `text` as a
// notification in the window, adds it to the API reply ("skipped_dialogs") and returns
// true; the caller skips its dialog and carries on as after OK. Otherwise returns false
// and does nothing, and the caller shows its dialog as before. `error`: an error box,
// shown as an error notification instead of a warning.
bool skip_dialog(const std::string &text, bool error = false);

// For a Yes/No question, when api_skips_dialogs(). Returns the answer: Yes only if the
// request said so in advance (DialogLog::answer_yes), otherwise No, and the question is
// then kept as the reason the request could not do what it asked
// (DialogLog::refused_question). The question and its answer are reported like
// skip_dialog().
bool api_question(const std::string &text);

// Slices: POST /slice marks the slice it starts (mark_api_slice(true); POST
// /slice/cancel clears the mark). Plater's slice-completed handler takes the mark once
// per completed slice: true means POST /slice started that slice, so a critical error
// goes to a notification and GET /slice/status instead of an error dialog that would
// open after the request returned. A slice started in the window keeps its dialog.
void mark_api_slice(bool started);
bool take_api_slice();

// --- For the API side (RemoteAPI*.cpp). GUI thread only.

// What the hooks above skipped during one API request's GUI work. Created at the start
// of that work and destroyed at its end; while it exists api_skips_dialogs() is true.
// Requests can nest (a wx event loop may run inside one), so a new log puts the outer
// one aside and gives it back when it is destroyed.
struct DialogLog
{
    DialogLog();
    ~DialogLog();
    DialogLog(const DialogLog &) = delete;
    DialogLog &operator=(const DialogLog &) = delete;

    std::vector<std::string> skipped;           // the texts, in order, for the reply
    std::vector<std::string> questions;         // every question asked, whatever the answer
    std::string              refused_question;  // the last question answered No
    bool                     answer_yes { false }; // set by a request to answer Yes to its questions
    bool                     quiet { false };      // a dry run: record only, no notification
    DialogLog               *outer { nullptr };    // the log put aside while this one is open
};

// The log of the request whose GUI work is running, or nullptr.
DialogLog *current_dialog_log();

}}} // namespace

#endif
