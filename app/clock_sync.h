/**
 * @file app/clock_sync.h
 * @brief The clock sync protocol Latency Cam uses to put its camera timestamps on the host's clock.
 *
 * The phone sends a request with its own time; the host answers with QueryPerformanceCounter values taken when the
 * request arrived and when the reply left. All fields are little endian.
 *  - Request, 16 bytes: magic "MLCS", sequence number (u32), t1 (i64, phone ns).
 *  - Reply, 40 bytes: magic "MLCR", sequence number, t1 echoed, t2 and t3 (i64, QPC ticks), QPC ticks per second.
 */
#pragma once

// standard includes
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

namespace latency_test {
  constexpr std::uint16_t clock_port = 48100;  ///< Default UDP port of the time server.
  constexpr std::uint32_t clock_request_magic = 0x53434C4D;  ///< "MLCS" read as a little endian u32.
  constexpr std::uint32_t clock_reply_magic = 0x52434C4D;  ///< "MLCR" read as a little endian u32.
  constexpr std::size_t clock_request_size = 16;  ///< Bytes in a request.
  constexpr std::size_t clock_reply_size = 40;  ///< Bytes in a reply.

  /**
   * @brief A clock sync request from the phone.
   */
  struct clock_request_t {
    std::uint32_t sequence;  ///< Request number, echoed in the reply.
    std::int64_t t1;  ///< Phone time when the request was sent, echoed in the reply.
  };

  /**
   * @brief Read a little endian unsigned value.
   *
   * @param data First byte.
   * @param bytes Number of bytes, at most 8.
   * @return The value.
   */
  constexpr std::uint64_t read_le(const std::uint8_t *data, std::size_t bytes) {
    std::uint64_t value = 0;
    for (std::size_t i = 0; i < bytes; ++i) {
      value |= static_cast<std::uint64_t>(data[i]) << (8 * i);
    }
    return value;
  }

  /**
   * @brief Write a little endian value.
   *
   * @param data First byte to write.
   * @param value Value to write.
   * @param bytes Number of bytes, at most 8.
   */
  constexpr void write_le(std::uint8_t *data, std::uint64_t value, std::size_t bytes) {
    for (std::size_t i = 0; i < bytes; ++i) {
      data[i] = static_cast<std::uint8_t>(value >> (8 * i));
    }
  }

  /**
   * @brief Parse a clock sync request.
   *
   * @param data Received bytes.
   * @param size Number of bytes received.
   * @return The request, or nothing when the packet is too short or has the wrong magic.
   */
  constexpr std::optional<clock_request_t> parse_clock_request(const std::uint8_t *data, std::size_t size) {
    if (size < clock_request_size || read_le(data, 4) != clock_request_magic) {
      return std::nullopt;
    }
    return clock_request_t {static_cast<std::uint32_t>(read_le(data + 4, 4)), static_cast<std::int64_t>(read_le(data + 8, 8))};
  }

  /**
   * @brief Build the reply to a clock sync request.
   *
   * @param request The request being answered.
   * @param t2 QPC value when the request arrived.
   * @param t3 QPC value when the reply is sent.
   * @param frequency QPC ticks per second.
   * @return The reply bytes.
   */
  constexpr std::array<std::uint8_t, clock_reply_size> make_clock_reply(const clock_request_t &request, std::int64_t t2, std::int64_t t3, std::int64_t frequency) {
    std::array<std::uint8_t, clock_reply_size> reply {};
    write_le(reply.data(), clock_reply_magic, 4);
    write_le(reply.data() + 4, request.sequence, 4);
    write_le(reply.data() + 8, static_cast<std::uint64_t>(request.t1), 8);
    write_le(reply.data() + 16, static_cast<std::uint64_t>(t2), 8);
    write_le(reply.data() + 24, static_cast<std::uint64_t>(t3), 8);
    write_le(reply.data() + 32, static_cast<std::uint64_t>(frequency), 8);
    return reply;
  }
}  // namespace latency_test
