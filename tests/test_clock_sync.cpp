/**
 * @file tests/test_clock_sync.cpp
 * @brief Test app/clock_sync.h.
 */

// test includes
#include <gtest/gtest.h>

// standard includes
#include <cstring>
#include <vector>

// local includes
#include <app/clock_sync.h>

namespace {
  /**
   * @brief Build a request the way Latency Cam sends it.
   *
   * @param magic First four bytes as a little endian u32.
   * @param sequence Sequence number.
   * @param t1 Phone time.
   * @return The request bytes.
   */
  std::vector<std::uint8_t> request(std::uint32_t magic, std::uint32_t sequence, std::int64_t t1) {
    std::vector<std::uint8_t> bytes(latency_test::clock_request_size);
    latency_test::write_le(bytes.data(), magic, 4);
    latency_test::write_le(bytes.data() + 4, sequence, 4);
    latency_test::write_le(bytes.data() + 8, static_cast<std::uint64_t>(t1), 8);
    return bytes;
  }
}  // namespace

TEST(ClockSyncTest, MagicsSpellTheProtocolNames) {
  std::uint8_t bytes[4];
  latency_test::write_le(bytes, latency_test::clock_request_magic, 4);
  EXPECT_EQ(std::memcmp(bytes, "MLCS", 4), 0);
  latency_test::write_le(bytes, latency_test::clock_reply_magic, 4);
  EXPECT_EQ(std::memcmp(bytes, "MLCR", 4), 0);
}

TEST(ClockSyncTest, ParsesARequest) {
  const auto bytes = request(latency_test::clock_request_magic, 7, 638549139378669);
  const auto parsed = latency_test::parse_clock_request(bytes.data(), bytes.size());
  ASSERT_TRUE(parsed);
  EXPECT_EQ(parsed->sequence, 7u);
  EXPECT_EQ(parsed->t1, 638549139378669);
}

TEST(ClockSyncTest, RejectsShortOrForeignPackets) {
  const auto bytes = request(latency_test::clock_request_magic, 1, 2);
  EXPECT_FALSE(latency_test::parse_clock_request(bytes.data(), bytes.size() - 1));
  const auto foreign = request(latency_test::clock_reply_magic, 1, 2);
  EXPECT_FALSE(latency_test::parse_clock_request(foreign.data(), foreign.size()));
}

TEST(ClockSyncTest, ReplyEchoesTheRequestAndCarriesTheHostTimes) {
  const latency_test::clock_request_t req {0xDEADBEEF, -5};
  const auto reply = latency_test::make_clock_reply(req, 0x0102030405060708, 0x1112131415161718, 10'000'000);
  EXPECT_EQ(latency_test::read_le(reply.data(), 4), latency_test::clock_reply_magic);
  EXPECT_EQ(latency_test::read_le(reply.data() + 4, 4), 0xDEADBEEFu);
  EXPECT_EQ(static_cast<std::int64_t>(latency_test::read_le(reply.data() + 8, 8)), -5);
  EXPECT_EQ(latency_test::read_le(reply.data() + 16, 8), 0x0102030405060708u);
  EXPECT_EQ(latency_test::read_le(reply.data() + 24, 8), 0x1112131415161718u);
  EXPECT_EQ(latency_test::read_le(reply.data() + 32, 8), 10'000'000u);
  EXPECT_EQ(reply[16], 0x08);  // little endian
}
