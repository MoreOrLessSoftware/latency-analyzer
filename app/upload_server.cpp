/**
 * @file app/upload_server.cpp
 * @brief HTTP server that receives Latency Cam recordings and runs the analyzer on them.
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

// local includes
#include "upload_server.h"

#include "http.h"

// platform includes (winsock2.h must come before windows.h)
#include <winsock2.h>
#include <windows.h>

// standard includes
#include <cstdio>
#include <deque>
#include <format>
#include <fstream>
#include <sstream>
#include <vector>

namespace latency_test {
  namespace {
    constexpr std::uint64_t max_upload = 8ull << 30;  ///< Largest file accepted, 8 GiB.

    /**
     * @brief Send all of a buffer.
     *
     * @param socket Connected socket.
     * @param data Bytes to send.
     * @return True when everything was sent.
     */
    bool send_all(SOCKET socket, std::string_view data) {
      while (!data.empty()) {
        const int sent = send(socket, data.data(), static_cast<int>(std::min<std::size_t>(data.size(), 1 << 20)), 0);
        if (sent <= 0) {
          return false;
        }
        data.remove_prefix(static_cast<std::size_t>(sent));
      }
      return true;
    }

    /**
     * @brief Send a complete response and close the connection afterwards.
     *
     * @param socket Connected socket.
     * @param status Status code and reason, e.g. "200 OK".
     * @param type Content-Type.
     * @param body Response body.
     */
    void respond(SOCKET socket, std::string_view status, std::string_view type, std::string_view body) {
      send_all(socket, std::format("HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", status, type, body.size()));
      send_all(socket, body);
    }

    /**
     * @brief Respond with a JSON error object.
     *
     * @param socket Connected socket.
     * @param status Status code and reason.
     * @param message What went wrong.
     */
    void respond_error(SOCKET socket, std::string_view status, std::string_view message) {
      respond(socket, status, "application/json", std::format("{{\"error\":\"{}\"}}", json_escape(message)));
    }

    /**
     * @brief Read a whole file.
     *
     * @param path File to read.
     * @param text Set to the contents.
     * @return True when the file could be read.
     */
    bool read_file(const std::filesystem::path &path, std::string &text) {
      std::ifstream in(path, std::ios::binary);
      if (!in) {
        return false;
      }
      std::ostringstream buffer;
      buffer << in.rdbuf();
      text = buffer.str();
      return true;
    }

    /**
     * @brief The last lines of a text file.
     *
     * @param path File to read.
     * @param count Number of lines.
     * @return The lines, joined with newlines.
     */
    std::string tail(const std::filesystem::path &path, std::size_t count) {
      std::ifstream in(path);
      std::deque<std::string> lines;
      std::string line;
      while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') {
          line.pop_back();
        }
        // Progress lines say nothing about why it failed
        if (line.find(" read ") != std::string::npos && line.find(" frames") != std::string::npos) {
          continue;
        }
        lines.push_back(line);
        if (lines.size() > count) {
          lines.pop_front();
        }
      }
      std::string out;
      for (const auto &l : lines) {
        out += l + "\n";
      }
      return out;
    }

    /**
     * @brief Quote a command line argument for CreateProcess.
     *
     * @param arg Argument.
     * @return The quoted argument.
     */
    std::wstring quote(const std::wstring &arg) {
      std::wstring out = L"\"";
      std::size_t backslashes = 0;
      for (const wchar_t c : arg) {
        if (c == L'\\') {
          ++backslashes;
          continue;
        }
        if (c == L'"') {
          out.append(backslashes * 2 + 1, L'\\');
        }
        else {
          out.append(backslashes, L'\\');
        }
        backslashes = 0;
        out += c;
      }
      out.append(backslashes * 2, L'\\');
      return out + L"\"";
    }

    /**
     * @brief Find an executable on PATH.
     *
     * @param name File name, e.g. python.exe.
     * @return Its full path, or empty.
     */
    std::filesystem::path search_path(const wchar_t *name) {
      wchar_t found[MAX_PATH] {};
      if (SearchPathW(nullptr, name, nullptr, MAX_PATH, found, nullptr) == 0) {
        return {};
      }
      return found;
    }

    /**
     * @brief Find analyze_video.py next to the executable, in analyzer\ beside it, or in ..\analyzer (a build folder).
     *
     * @return Its path, or empty.
     */
    std::filesystem::path find_analyzer() {
      wchar_t module[MAX_PATH] {};
      GetModuleFileNameW(nullptr, module, MAX_PATH);
      const std::filesystem::path dir = std::filesystem::path(module).parent_path();
      for (const auto &candidate : {dir / "analyze_video.py", dir / "analyzer" / "analyze_video.py", dir.parent_path() / "analyzer" / "analyze_video.py"}) {
        std::error_code error;
        if (std::filesystem::is_regular_file(candidate, error)) {
          return candidate;
        }
      }
      return {};
    }
  }  // namespace

  upload_server_t::~upload_server_t() {
    stop();
    std::lock_guard lock(mutex_);
    for (auto &[name, handle] : analyses_) {
      CloseHandle(handle);
    }
    analyses_.clear();
  }

  std::string upload_server_t::start(int port, analysis_config_t config) {
    config_ = std::move(config);
    std::error_code error;
    std::filesystem::create_directories(config_.results_dir, error);
    if (error) {
      return "cannot create " + config_.results_dir.string() + ": " + error.message();
    }

    const SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listener == INVALID_SOCKET) {
      return std::format("socket failed ({})", WSAGetLastError());
    }
    sockaddr_in address {};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<u_short>(port));
    address.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listener, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) == SOCKET_ERROR || listen(listener, 4) == SOCKET_ERROR) {
      const int code = WSAGetLastError();
      closesocket(listener);
      return std::format("TCP port {} is in use or blocked ({})", port, code);
    }
    listener_ = listener;
    running_ = true;
    thread_ = std::thread(&upload_server_t::run, this);
    return {};
  }

  void upload_server_t::stop() {
    running_ = false;
    if (listener_ != ~std::uintptr_t {0}) {
      closesocket(static_cast<SOCKET>(listener_));
      listener_ = ~std::uintptr_t {0};
    }
    if (thread_.joinable()) {
      thread_.join();
    }
  }

  void upload_server_t::run() {
    while (running_) {
      const SOCKET client = accept(static_cast<SOCKET>(listener_), nullptr, nullptr);
      if (client == INVALID_SOCKET) {
        if (!running_) {
          break;
        }
        continue;
      }
      // A stalled phone must not hold the server forever
      const DWORD timeout_ms = 30'000;
      setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char *>(&timeout_ms), sizeof(timeout_ms));
      setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char *>(&timeout_ms), sizeof(timeout_ms));
      serve(client);
      shutdown(client, SD_BOTH);
      closesocket(client);
    }
  }

  void upload_server_t::serve(std::uintptr_t client_handle) {
    const auto client = static_cast<SOCKET>(client_handle);

    // Read the head; whatever comes after the blank line is the start of the body
    std::string data;
    std::size_t head_end = std::string::npos;
    char buffer[64 * 1024];
    while ((head_end = data.find("\r\n\r\n")) == std::string::npos) {
      if (data.size() > max_head_size) {
        respond_error(client, "431 Request Header Fields Too Large", "request head too large");
        return;
      }
      const int received = recv(client, buffer, sizeof(buffer), 0);
      if (received <= 0) {
        return;
      }
      data.append(buffer, static_cast<std::size_t>(received));
    }
    const auto request = parse_request_head(std::string_view(data).substr(0, head_end));
    if (!request) {
      respond_error(client, "400 Bad Request", "malformed request");
      return;
    }
    std::string body_start = data.substr(head_end + 4);
    const route_t route = route_request(request->method, request->path);

    switch (route.kind) {
      case route_e::index:
        respond(client, "200 OK", "text/plain", "latency-test: Latency Cam uploads go to /recordings/\n");
        return;

      case route_e::upload_video:
      case route_e::upload_timing:
        {
          if (!request->content_length) {
            respond_error(client, "411 Length Required", "Content-Length is required");
            return;
          }
          const std::uint64_t length = *request->content_length;
          if (length > max_upload) {
            respond_error(client, "413 Content Too Large", "file too large");
            return;
          }
          if (request->expect_continue) {
            send_all(client, "HTTP/1.1 100 Continue\r\n\r\n");
          }
          const auto final_path = config_.results_dir / (route.name + (route.kind == route_e::upload_video ? ".mp4" : ".camera.json"));
          auto part_path = final_path;
          part_path += L".part";

          // Marked busy until the file is in place, however this ends, so closing the app can warn about it
          uploading_ = true;

          struct clear_t {
            std::atomic<bool> &flag;  ///< Flag to clear.

            ~clear_t() {
              flag = false;
            }
          } clear_uploading {uploading_};

          {
            std::ofstream out(part_path, std::ios::binary | std::ios::trunc);
            if (!out) {
              respond_error(client, "500 Internal Server Error", "cannot write " + part_path.string());
              return;
            }
            std::uint64_t remaining = length;
            const std::size_t first = static_cast<std::size_t>(std::min<std::uint64_t>(body_start.size(), remaining));
            out.write(body_start.data(), static_cast<std::streamsize>(first));
            remaining -= first;
            while (remaining > 0) {
              const int received = recv(client, buffer, static_cast<int>(std::min<std::uint64_t>(sizeof(buffer), remaining)), 0);
              if (received <= 0) {
                out.close();
                std::error_code ignored;
                std::filesystem::remove(part_path, ignored);
                return;
              }
              out.write(buffer, received);
              remaining -= static_cast<std::uint64_t>(received);
            }
            if (!out.flush()) {
              respond_error(client, "500 Internal Server Error", "write failed");
              return;
            }
          }
          std::error_code error;
          std::filesystem::rename(part_path, final_path, error);
          if (error) {
            respond_error(client, "500 Internal Server Error", error.message());
            return;
          }
          respond(client, "201 Created", "application/json", std::format("{{\"saved\":{}}}", length));
          return;
        }

      case route_e::analyze:
        {
          std::string error;
          if (!start_analysis(route.name, error)) {
            respond_error(client, "409 Conflict", error);
            return;
          }
          respond(client, "202 Accepted", "application/json", status_json(route.name));
          return;
        }

      case route_e::status:
        respond(client, "200 OK", "application/json", status_json(route.name));
        return;

      case route_e::report:
        {
          std::string html;
          if (!read_file(config_.results_dir / (route.name + ".report.html"), html)) {
            respond_error(client, "404 Not Found", "no report for " + route.name);
            return;
          }
          respond(client, "200 OK", "text/html; charset=utf-8", html);
          return;
        }

      case route_e::not_found:
        respond_error(client, "404 Not Found", "unknown route");
        return;
    }
  }

  bool upload_server_t::start_analysis(const std::string &name, std::string &error) {
    std::lock_guard lock(mutex_);
    if (const auto it = analyses_.find(name); it != analyses_.end()) {
      if (WaitForSingleObject(it->second, 0) == WAIT_TIMEOUT) {
        return true;  // Already running
      }
      CloseHandle(it->second);
      analyses_.erase(it);
    }

    const auto video = config_.results_dir / (name + ".mp4");
    const auto timing = config_.results_dir / (name + ".camera.json");
    std::error_code fs_error;
    if (!std::filesystem::is_regular_file(video, fs_error) || !std::filesystem::is_regular_file(timing, fs_error)) {
      error = "upload " + name + ".mp4 and " + name + ".camera.json first";
      return false;
    }
    const auto analyzer = config_.analyzer.empty() ? find_analyzer() : config_.analyzer;
    if (analyzer.empty()) {
      error = "analyze_video.py was not found next to latency-test.exe; pass --analyzer";
      return false;
    }
    std::wstring python = config_.python.wstring();
    std::wstring python_args;
    if (python.empty()) {
      python = search_path(L"python.exe").wstring();
      if (python.empty()) {
        python = search_path(L"py.exe").wstring();
        python_args = L" -3";
      }
    }
    if (python.empty()) {
      error = "Python was not found on PATH; pass --python";
      return false;
    }
    if (config_.flush_logs) {
      config_.flush_logs();
    }

    // Keep a copy of the logs with the recording: the app starts them afresh each run, so without it the recording
    // could not be analysed again later
    const auto host_log = config_.results_dir / (name + ".host.csv");
    const auto host_frames_log = config_.results_dir / (name + ".host.frames.csv");
    // The same rule as frames_log_path() in pattern.h: .csv becomes .frames.csv, anything else gets it appended
    auto frames_source = config_.host_log;
    if (frames_source.extension() == L".csv") {
      frames_source.replace_extension();
    }
    frames_source += L".frames.csv";
    std::filesystem::copy_file(config_.host_log, host_log, std::filesystem::copy_options::overwrite_existing, fs_error);
    if (!fs_error) {
      std::filesystem::copy_file(frames_source, host_frames_log, std::filesystem::copy_options::overwrite_existing, fs_error);
    }
    if (fs_error) {
      error = "could not copy the host logs: " + fs_error.message();
      return false;
    }

    std::wstring command = quote(python) + python_args + L" -u " + quote(analyzer.wstring()) + L" " + quote(video.wstring()) +
                           std::format(L" --stream {}x{}", config_.width, config_.height) + L" --host-log " + quote(host_log.wstring()) +
                           L" --out " + quote(config_.results_dir.wstring()) + L" --report";
    if (config_.fps > 0) {
      command += std::format(L" --stream-fps {}", config_.fps);
    }

    // The analyzer's output goes to NAME.analysis.txt, which a failed status quotes
    SECURITY_ATTRIBUTES inherit {sizeof(inherit), nullptr, TRUE};
    const auto log_path = config_.results_dir / (name + ".analysis.txt");
    const HANDLE log = CreateFileW(log_path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, &inherit, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (log == INVALID_HANDLE_VALUE) {
      error = "cannot write " + log_path.string();
      return false;
    }
    STARTUPINFOW startup {};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdOutput = log;
    startup.hStdError = log;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    PROCESS_INFORMATION process {};
    std::vector<wchar_t> command_line(command.begin(), command.end());
    command_line.push_back(L'\0');
    const BOOL started = CreateProcessW(nullptr, command_line.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS, nullptr, config_.results_dir.c_str(), &startup, &process);
    CloseHandle(log);
    if (!started) {
      error = std::format("could not start Python ({})", GetLastError());
      return false;
    }
    CloseHandle(process.hThread);
    std::error_code ignored;
    std::filesystem::remove(config_.results_dir / (name + ".summary.json"), ignored);
    analyses_[name] = process.hProcess;
    return true;
  }

  std::string upload_server_t::busy_description() {
    std::vector<std::string> analysing;
    {
      std::lock_guard lock(mutex_);
      for (const auto &[name, handle] : analyses_) {
        if (WaitForSingleObject(handle, 0) == WAIT_TIMEOUT) {
          analysing.push_back(name);
        }
      }
    }
    std::string text;
    if (uploading_) {
      text = "is still receiving a recording from Latency Cam";
    }
    if (!analysing.empty()) {
      text += text.empty() ? "is still analysing " : ", and analysing ";
      for (std::size_t i = 0; i < analysing.size(); ++i) {
        text += (i == 0 ? "" : i + 1 == analysing.size() ? " and " : ", ") + analysing[i];
      }
    }
    return text;
  }

  std::string upload_server_t::status_json(const std::string &name) {
    const auto summary_path = config_.results_dir / (name + ".summary.json");
    const auto report_url = "/recordings/" + name + "/report";
    {
      std::lock_guard lock(mutex_);
      if (const auto it = analyses_.find(name); it != analyses_.end()) {
        if (WaitForSingleObject(it->second, 0) == WAIT_TIMEOUT) {
          return "{\"state\":\"analyzing\"}";
        }
        DWORD code = 1;
        GetExitCodeProcess(it->second, &code);
        if (code != 0) {
          return std::format("{{\"state\":\"failed\",\"exit_code\":{},\"output\":\"{}\"}}", code, json_escape(tail(config_.results_dir / (name + ".analysis.txt"), 15)));
        }
      }
    }
    std::string summary;
    if (read_file(summary_path, summary)) {
      return std::format("{{\"state\":\"done\",\"report\":\"{}\",\"summary\":{}}}", json_escape(report_url), summary);
    }
    std::error_code error;
    const bool uploaded = std::filesystem::is_regular_file(config_.results_dir / (name + ".mp4"), error) &&
                          std::filesystem::is_regular_file(config_.results_dir / (name + ".camera.json"), error);
    return uploaded ? "{\"state\":\"uploaded\"}" : "{\"state\":\"missing\"}";
  }
}  // namespace latency_test
