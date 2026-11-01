/**
 * @file app/upload_server.h
 * @brief HTTP server that receives Latency Cam recordings and runs the analyzer on them (see http.h).
 */
#pragma once

// standard includes
#include <atomic>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>

namespace latency_test {
  /**
   * @brief How to run the analyzer on an uploaded recording.
   */
  struct analysis_config_t {
    std::filesystem::path results_dir;  ///< Where recordings and results are saved.
    std::filesystem::path python;  ///< Python interpreter; empty to find python.exe or py.exe on PATH.
    std::filesystem::path analyzer;  ///< analyze_video.py; empty to look next to the executable.
    std::filesystem::path host_log;  ///< The test app's input log, with its .frames.csv next to it.
    int width = 0;  ///< Pattern width in pixels, passed as --stream.
    int height = 0;  ///< Pattern height in pixels.
    double fps = 0;  ///< Frame rate passed as --stream-fps; 0 to leave it out.
    std::function<void()> flush_logs;  ///< Called before the analyzer starts, so it reads complete logs.
  };

  /**
   * @brief Accepts one connection at a time on a TCP port and serves the routes in http.h.
   */
  class upload_server_t {
  public:
    upload_server_t() = default;
    upload_server_t(const upload_server_t &) = delete;
    upload_server_t &operator=(const upload_server_t &) = delete;

    /**
     * @brief Stop the server if it is running.
     */
    ~upload_server_t();

    /**
     * @brief Start listening. Winsock must already be started.
     *
     * @param port TCP port.
     * @param config How to run the analyzer.
     * @return Empty on success, otherwise what went wrong.
     */
    std::string start(int port, analysis_config_t config);

    /**
     * @brief Stop listening and wait for the server thread. Analyses already running carry on.
     */
    void stop();

  private:
    /**
     * @brief Accept and serve connections until stopped.
     */
    void run();

    /**
     * @brief Serve one connection.
     *
     * @param client Connected socket, as an integer so this header does not need winsock2.h.
     */
    void serve(std::uintptr_t client);

    /**
     * @brief Start the analyzer on a recording.
     *
     * @param name Recording name.
     * @param error Set to what went wrong when it could not start.
     * @return True when it started.
     */
    bool start_analysis(const std::string &name, std::string &error);

    /**
     * @brief Describe a recording's analysis as JSON.
     *
     * @param name Recording name.
     * @return The status object.
     */
    std::string status_json(const std::string &name);

    analysis_config_t config_;  ///< How to run the analyzer.
    std::uintptr_t listener_ = ~std::uintptr_t {0};  ///< Listening socket.
    std::thread thread_;  ///< Accept loop.
    std::atomic<bool> running_ {false};  ///< Cleared to stop the accept loop.
    std::mutex mutex_;  ///< Guards analyses_.
    std::map<std::string, void *> analyses_;  ///< Process handle of each analysis started, by recording name.
  };
}  // namespace latency_test
