/**
 * @file app/http.h
 * @brief The small HTTP API Latency Cam uses to hand its recordings to the test app.
 *
 * Routes, all under /recordings/:
 *  - PUT NAME.mp4 and PUT NAME.camera.json save a recording's two files.
 *  - POST NAME/analyze starts the analyzer on them.
 *  - GET NAME/status reports the analysis: analyzing, done (with the summary) or failed.
 *  - GET NAME/report serves the HTML report.
 *
 * Names are limited to letters, digits, '.', '_' and '-' so a request can never reach outside the results folder.
 */
#pragma once

// standard includes
#include <algorithm>
#include <cctype>
#include <charconv>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace latency_test {
  constexpr std::size_t max_head_size = 16 * 1024;  ///< Longest request line and headers accepted.

  /**
   * @brief The request line and the headers this server cares about.
   */
  struct http_request_t {
    std::string method;  ///< Request method, e.g. PUT.
    std::string path;  ///< Request path without the query string.
    std::optional<std::uint64_t> content_length;  ///< Content-Length, when present and valid.
    bool expect_continue = false;  ///< Whether the client waits for 100 Continue before sending the body.
  };

  /**
   * @brief What a request asks for.
   */
  enum class route_e {
    upload_video,  ///< PUT NAME.mp4
    upload_timing,  ///< PUT NAME.camera.json
    analyze,  ///< POST NAME/analyze
    status,  ///< GET NAME/status
    report,  ///< GET NAME/report
    index,  ///< GET /
    not_found,  ///< Anything else
  };

  /**
   * @brief A route with the recording name it applies to.
   */
  struct route_t {
    route_e kind;  ///< What is asked for.
    std::string name;  ///< Recording name, without extension; empty for index and not_found.
  };

  /**
   * @brief Compare two strings ignoring ASCII case.
   *
   * @param a First string.
   * @param b Second string.
   * @return True when they match.
   */
  inline bool iequals(std::string_view a, std::string_view b) {
    return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](char x, char y) {
             return std::tolower(static_cast<unsigned char>(x)) == std::tolower(static_cast<unsigned char>(y));
           });
  }

  /**
   * @brief Trim spaces and tabs from both ends.
   *
   * @param text Text to trim.
   * @return The trimmed text.
   */
  inline std::string_view trim(std::string_view text) {
    while (!text.empty() && (text.front() == ' ' || text.front() == '\t')) {
      text.remove_prefix(1);
    }
    while (!text.empty() && (text.back() == ' ' || text.back() == '\t' || text.back() == '\r')) {
      text.remove_suffix(1);
    }
    return text;
  }

  /**
   * @brief Parse a request head: the request line and headers, up to but not including the blank line.
   *
   * @param head Request head.
   * @return The request, or nothing when the request line is malformed.
   */
  inline std::optional<http_request_t> parse_request_head(std::string_view head) {
    http_request_t request;
    const auto line_end = head.find("\r\n");
    const std::string_view line = head.substr(0, line_end);
    const auto first_space = line.find(' ');
    const auto second_space = first_space == std::string_view::npos ? std::string_view::npos : line.find(' ', first_space + 1);
    if (first_space == std::string_view::npos || second_space == std::string_view::npos || line.substr(second_space + 1).rfind("HTTP/", 0) != 0) {
      return std::nullopt;
    }
    request.method = std::string(line.substr(0, first_space));
    std::string_view target = line.substr(first_space + 1, second_space - first_space - 1);
    target = target.substr(0, target.find('?'));
    if (target.empty() || target.front() != '/') {
      return std::nullopt;
    }
    request.path = std::string(target);

    std::string_view rest = line_end == std::string_view::npos ? std::string_view {} : head.substr(line_end + 2);
    while (!rest.empty()) {
      const auto end = rest.find("\r\n");
      const std::string_view header = rest.substr(0, end);
      rest = end == std::string_view::npos ? std::string_view {} : rest.substr(end + 2);
      const auto colon = header.find(':');
      if (colon == std::string_view::npos) {
        continue;
      }
      const std::string_view key = trim(header.substr(0, colon));
      const std::string_view value = trim(header.substr(colon + 1));
      if (iequals(key, "Content-Length")) {
        std::uint64_t length {};
        const auto [ptr, error] = std::from_chars(value.data(), value.data() + value.size(), length);
        if (error == std::errc {} && ptr == value.data() + value.size()) {
          request.content_length = length;
        }
      }
      else if (iequals(key, "Expect") && iequals(value, "100-continue")) {
        request.expect_continue = true;
      }
    }
    return request;
  }

  /**
   * @brief Check that a recording name is safe to use as a file name.
   *
   * @param name Name to check.
   * @return True when it is 1 to 200 letters, digits, '.', '_' or '-', and does not start with '.'.
   */
  inline bool valid_name(std::string_view name) {
    if (name.empty() || name.size() > 200 || name.front() == '.') {
      return false;
    }
    return std::all_of(name.begin(), name.end(), [](char c) {
      return std::isalnum(static_cast<unsigned char>(c)) || c == '.' || c == '_' || c == '-';
    });
  }

  /**
   * @brief Work out what a request asks for.
   *
   * @param method Request method.
   * @param path Request path.
   * @return The route, with not_found for anything unknown or with an unsafe name.
   */
  inline route_t route_request(std::string_view method, std::string_view path) {
    constexpr std::string_view prefix = "/recordings/";
    if (method == "GET" && path == "/") {
      return {route_e::index, {}};
    }
    if (path.rfind(prefix, 0) != 0) {
      return {route_e::not_found, {}};
    }
    const std::string_view rest = path.substr(prefix.size());
    const auto ends_with = [&](std::string_view suffix) {
      return rest.size() > suffix.size() && rest.substr(rest.size() - suffix.size()) == suffix;
    };
    const auto make = [](route_e kind, std::string_view name) {
      return valid_name(name) ? route_t {kind, std::string(name)} : route_t {route_e::not_found, {}};
    };
    if (method == "PUT" && ends_with(".camera.json")) {
      return make(route_e::upload_timing, rest.substr(0, rest.size() - 12));
    }
    if (method == "PUT" && ends_with(".mp4")) {
      return make(route_e::upload_video, rest.substr(0, rest.size() - 4));
    }
    if (method == "POST" && ends_with("/analyze")) {
      return make(route_e::analyze, rest.substr(0, rest.size() - 8));
    }
    if (method == "GET" && ends_with("/status")) {
      return make(route_e::status, rest.substr(0, rest.size() - 7));
    }
    if (method == "GET" && ends_with("/report")) {
      return make(route_e::report, rest.substr(0, rest.size() - 7));
    }
    return {route_e::not_found, {}};
  }

  /**
   * @brief Find the analyzer's latest progress line, "read 1500/2488 frames", in the end of its output.
   *
   * @param output The analyzer's output, or its last few kilobytes.
   * @param done Set to the frames read.
   * @param total Set to the frames in the video.
   * @return True when a progress line was found.
   */
  inline bool parse_analysis_progress(std::string_view output, int &done, int &total) {
    constexpr std::string_view marker = "read ";
    for (auto at = output.rfind(marker); at != std::string_view::npos; at = at == 0 ? std::string_view::npos : output.rfind(marker, at - 1)) {
      const char *p = output.data() + at + marker.size();
      const char *end = output.data() + output.size();
      int a = 0;
      int b = 0;
      const auto first = std::from_chars(p, end, a);
      if (first.ec != std::errc {} || first.ptr == end || *first.ptr != '/') {
        continue;
      }
      const auto second = std::from_chars(first.ptr + 1, end, b);
      if (second.ec != std::errc {} || b <= 0 || std::string_view(second.ptr, end - second.ptr).rfind(" frames", 0) != 0) {
        continue;
      }
      done = a;
      total = b;
      return true;
    }
    return false;
  }

  /**
   * @brief Escape text for a JSON string.
   *
   * @param text Text to escape.
   * @return The escaped text, without quotes.
   */
  inline std::string json_escape(std::string_view text) {
    std::string out;
    for (const char c : text) {
      switch (c) {
        case '"':
          out += "\\\"";
          break;
        case '\\':
          out += "\\\\";
          break;
        case '\n':
          out += "\\n";
          break;
        case '\r':
          out += "\\r";
          break;
        case '\t':
          out += "\\t";
          break;
        default:
          if (static_cast<unsigned char>(c) < 0x20) {
            constexpr char hex[] = "0123456789abcdef";
            out += "\\u00";
            out += hex[(c >> 4) & 0xF];
            out += hex[c & 0xF];
          }
          else {
            out += c;
          }
      }
    }
    return out;
  }
}  // namespace latency_test
