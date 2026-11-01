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
    std::filesystem::path analyzer;  ///< latency-analyzer.exe or analyze_video.py; empty to look next to the executable.
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

    /**
     * @brief What the server is doing that closing the app would cut short.
     */
    struct activity_t {
      bool busy = false;  ///< Whether anything is in progress.
      std::string description;  ///< What, e.g. "Analysing run_1: reading frames 1500 of 2488".
      int percent = -1;  ///< Progress from 0 to 100, or -1 when it can't be told.
    };

    /**
     * @brief Describe what is in progress: a recording being received, an analysis running, an analysis Latency Cam
     * hasn't downloaded the report of yet, or a video just uploaded whose analysis Latency Cam is about to start. The
     * last two only count for 15 s, in case the phone has gone away.
     *
     * @return The activity; busy is false when the app can close without cutting anything short.
     */
    activity_t activity();

  private:
    /**
     * @brief One analysis started by the server.
     */
    struct analysis_t {
      void *process = nullptr;  ///< Process handle.
      std::uint64_t finished_ms = 0;  ///< GetTickCount64 when it was first seen finished; 0 while running.
      bool delivered = false;  ///< Whether Latency Cam has downloaded the report (or seen that the analysis failed).
    };

    /**
     * @brief The folder a recording's files go in.
     *
     * @param name Recording name.
     * @return results_dir/name.
     */
    std::filesystem::path run_dir(const std::string &name) const;

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
    std::mutex mutex_;  ///< Guards everything below.
    std::map<std::string, analysis_t> analyses_;  ///< Each analysis started, by recording name.
    std::string upload_name_;  ///< File being received; empty when none.
    std::uint64_t upload_received_ = 0;  ///< Bytes of it received so far.
    std::uint64_t upload_total_ = 0;  ///< Its size.
    std::string awaiting_analysis_;  ///< Recording whose video arrived but whose analysis hasn't been started.
    std::uint64_t awaiting_since_ms_ = 0;  ///< GetTickCount64 when that video arrived.
  };
}  // namespace latency_test
