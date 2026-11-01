/**
 * @file tests/test_pattern.cpp
 * @brief Test app/pattern.h.
 */

// test includes
#include <gtest/gtest.h>

// standard includes
#include <bit>

// local includes
#include <app/pattern.h>

namespace {
  using latency_test::rect_t;

  /**
   * @brief Check whether two rectangles overlap.
   *
   * @param a First rectangle.
   * @param b Second rectangle.
   * @return True when they share at least one pixel.
   */
  bool overlaps(const rect_t &a, const rect_t &b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  /**
   * @brief Check whether a rectangle lies fully inside another.
   *
   * @param outer Containing rectangle.
   * @param inner Contained rectangle.
   * @return True when every pixel of inner is inside outer.
   */
  bool encloses(const rect_t &outer, const rect_t &inner) {
    return inner.x >= outer.x && inner.y >= outer.y && inner.x + inner.w <= outer.x + outer.w && inner.y + inner.h <= outer.y + outer.h;
  }

  /**
   * @brief Parse a command line given as separate arguments.
   *
   * @param args Arguments.
   * @param error Set when parsing fails.
   * @return The parsed options.
   */
  std::optional<latency_test::options_t> parse(std::vector<std::string_view> args, std::string &error) {
    return latency_test::parse_options(args, error);
  }
}  // namespace

TEST(LatencyTestPatternTest, GrayCodeRoundTripsAndChangesOneBitAtATime) {
  for (std::uint32_t value = 0; value < 1024; ++value) {
    EXPECT_EQ(latency_test::gray_decode(latency_test::gray_encode(value)), value);
    const std::uint32_t changed = latency_test::gray_encode(value) ^ latency_test::gray_encode(value + 1);
    EXPECT_EQ(std::popcount(changed), 1) << value;
  }
  EXPECT_EQ(latency_test::gray_decode(latency_test::gray_encode(0xFFFFFFFFu)), 0xFFFFFFFFu);
}

TEST(LatencyTestPatternTest, BitLitReadsGrayCodeMostSignificantFirst) {
  // 2 is Gray 0b0011 over four bits.
  EXPECT_FALSE(latency_test::bit_lit(2, 4, 0));
  EXPECT_FALSE(latency_test::bit_lit(2, 4, 1));
  EXPECT_TRUE(latency_test::bit_lit(2, 4, 2));
  EXPECT_TRUE(latency_test::bit_lit(2, 4, 3));
}

TEST(LatencyTestPatternTest, BitRowCountsUpThroughWraps) {
  const auto row = [](std::uint32_t value) {
    std::uint32_t code = 0;
    for (int i = 0; i < latency_test::input_bit_count; ++i) {
      code = (code << 1) | latency_test::bit_lit(value, latency_test::input_bit_count, i);
    }
    return code;
  };
  for (std::uint32_t value = 0; value < 64; ++value) {
    // Reads back as the value modulo the row size, so a camera sees it count up by one every time
    EXPECT_EQ(latency_test::gray_decode(row(value)), value % 16) << value;
    EXPECT_EQ(std::popcount(row(value) ^ row(value + 1)), 1) << value;
  }
}

TEST(LatencyTestPatternTest, RectContainsIsHalfOpen) {
  constexpr rect_t rect {10, 20, 5, 5};
  EXPECT_TRUE(rect.contains(10, 20));
  EXPECT_TRUE(rect.contains(14, 24));
  EXPECT_FALSE(rect.contains(15, 24));
  EXPECT_FALSE(rect.contains(14, 25));
  EXPECT_FALSE(rect.contains(9, 20));
}

TEST(LatencyTestPatternTest, Layout1080pIsGridAlignedAndCentered) {
  const auto layout = latency_test::compute_layout(1920, 1080);
  EXPECT_EQ(layout.grid, 64);
  EXPECT_EQ(layout.cell, 128);
  EXPECT_EQ(layout.trigger, (rect_t {832, 384, 256, 256}));
  EXPECT_EQ(layout.frame_bits[0], (rect_t {704, 256, 64, 64}));
  EXPECT_EQ(layout.input_bits[0], (rect_t {704, 704, 128, 64}));
  EXPECT_EQ(layout.fiducials[3], (rect_t {1728, 832, 128, 128}));
  EXPECT_EQ(layout.readout, (rect_t {256, 832, 1408, 128}));
  EXPECT_EQ(layout.info, (rect_t {256, 64, 1408, 128}));
  EXPECT_EQ(layout.digit_w, 48);
  EXPECT_EQ(layout.digit_x, 256 + (1408 - 768) / 2);
  EXPECT_EQ(layout.digit_y, 832 + 16);
}

class LatencyTestLayoutTest: public ::testing::TestWithParam<std::pair<int, int>> {};

TEST_P(LatencyTestLayoutTest, ElementsFitDoNotOverlapAndStayOutOfTheNoise) {
  const auto [width, height] = GetParam();
  const auto layout = latency_test::compute_layout(width, height);
  const rect_t screen {0, 0, width, height};

  std::vector<rect_t> patches {layout.trigger};
  patches.insert(patches.end(), layout.frame_bits.begin(), layout.frame_bits.end());
  patches.insert(patches.end(), layout.input_bits.begin(), layout.input_bits.end());

  for (const auto &patch : patches) {
    EXPECT_TRUE(encloses(layout.quiet, patch));
    EXPECT_EQ(patch.x % 16, 0);
    EXPECT_EQ(patch.y % 16, 0);
  }
  for (std::size_t a = 0; a < patches.size(); ++a) {
    for (std::size_t b = a + 1; b < patches.size(); ++b) {
      EXPECT_FALSE(overlaps(patches[a], patches[b])) << a << ' ' << b;
    }
  }

  EXPECT_TRUE(encloses(screen, layout.quiet));
  EXPECT_TRUE(encloses(screen, layout.readout));
  EXPECT_TRUE(encloses(screen, layout.info));
  EXPECT_FALSE(overlaps(layout.readout, layout.quiet));
  EXPECT_FALSE(overlaps(layout.info, layout.quiet));
  EXPECT_FALSE(overlaps(layout.info, layout.readout));
  for (const auto &fiducial : layout.fiducials) {
    EXPECT_TRUE(encloses(screen, fiducial));
    EXPECT_FALSE(overlaps(fiducial, layout.quiet));
    EXPECT_FALSE(overlaps(fiducial, layout.readout));
    EXPECT_FALSE(overlaps(fiducial, layout.info));
  }

  // Every digit slot fits inside the readout band
  const int digits_width = latency_test::readout_slot_count * latency_test::readout_advance(layout.digit_w) - layout.digit_w / 4;
  const rect_t digits {layout.digit_x, layout.digit_y, digits_width, 2 * layout.digit_w};
  EXPECT_GE(layout.digit_w, 4);
  EXPECT_EQ(layout.digit_w % 4, 0);
  EXPECT_TRUE(encloses(layout.readout, digits));
}

INSTANTIATE_TEST_SUITE_P(
  ScreenSizes,
  LatencyTestLayoutTest,
  ::testing::Values(
    std::make_pair(1280, 720),
    std::make_pair(1920, 1080),
    std::make_pair(2560, 1440),
    std::make_pair(3840, 2160),
    std::make_pair(3440, 1440),
    std::make_pair(1080, 2400),
    std::make_pair(800, 600),
    std::make_pair(320, 240)
  )
);

TEST(LatencyTestPatternTest, ParseOptionsDefaults) {
  std::string error;
  const auto options = parse({}, error);
  ASSERT_TRUE(options);
  EXPECT_FALSE(options->windowed);
  EXPECT_FALSE(options->vsync);
  EXPECT_FALSE(options->help);
  EXPECT_DOUBLE_EQ(options->noise, 0.5);
  EXPECT_DOUBLE_EQ(options->fps, 0);
  EXPECT_TRUE(options->log_path.empty());
}

TEST(LatencyTestPatternTest, ParseOptionsReadsEveryOption) {
  std::string error;
  const auto options = parse({"--windowed", "--vsync", "--noise", "0", "--fps", "120", "--log", "C:\\x.csv", "--help"}, error);
  ASSERT_TRUE(options) << error;
  EXPECT_TRUE(options->windowed);
  EXPECT_TRUE(options->vsync);
  EXPECT_TRUE(options->help);
  EXPECT_DOUBLE_EQ(options->noise, 0);
  EXPECT_DOUBLE_EQ(options->fps, 120);
  EXPECT_EQ(options->log_path, "C:\\x.csv");

  EXPECT_DOUBLE_EQ(parse({"--no-noise"}, error)->noise, 0);
  EXPECT_DOUBLE_EQ(parse({"--noise", "0.8", "--no-noise"}, error)->noise, 0);
  EXPECT_TRUE(parse({"-h"}, error)->help);
  EXPECT_TRUE(parse({"/?"}, error)->help);
}

TEST(LatencyTestPatternTest, ParseOptionsRejectsBadInput) {
  const std::vector<std::pair<std::vector<std::string_view>, std::string>> cases {
    {{"--bogus"}, "Unknown option --bogus"},
    {{"--noise"}, "--noise needs a value"},
    {{"--fps"}, "--fps needs a value"},
    {{"--log"}, "--log needs a value"},
    {{"--noise", "1.5"}, "--noise must be between 0 and 1"},
    {{"--noise", "abc"}, "--noise must be between 0 and 1"},
    {{"--fps", "0"}, "--fps must be between 1 and 1000"},
    {{"--fps", "60x"}, "--fps must be between 1 and 1000"},
  };
  for (const auto &[args, expected] : cases) {
    std::string error;
    EXPECT_FALSE(parse(args, error));
    EXPECT_EQ(error, expected);
  }
}

TEST(LatencyTestPatternTest, ParseOptionsReadsAutoAndTimeServer) {
  std::string error;
  auto options = parse({}, error);
  ASSERT_TRUE(options);
  EXPECT_FALSE(options->auto_flip);
  EXPECT_DOUBLE_EQ(options->auto_min_ms, 150);
  EXPECT_DOUBLE_EQ(options->auto_max_ms, 400);
  EXPECT_EQ(options->time_port, 48100);

  EXPECT_TRUE(parse({"--auto"}, error)->auto_flip);
  options = parse({"--auto-interval", "100-300", "--time-port", "50000"}, error);
  ASSERT_TRUE(options) << error;
  EXPECT_TRUE(options->auto_flip);
  EXPECT_DOUBLE_EQ(options->auto_min_ms, 100);
  EXPECT_DOUBLE_EQ(options->auto_max_ms, 300);
  EXPECT_EQ(options->time_port, 50000);
  EXPECT_DOUBLE_EQ(parse({"--auto-interval", "250-250"}, error)->auto_max_ms, 250);
  EXPECT_EQ(parse({"--no-time-server"}, error)->time_port, 0);
  EXPECT_EQ(parse({"--time-port", "0"}, error)->time_port, 0);
}

TEST(LatencyTestPatternTest, ParseOptionsRejectsBadAutoAndTimeServer) {
  const std::string interval_error = "--auto-interval must be <min>-<max> in ms, between 20 and 10000";
  const std::string port_error = "--time-port must be a port number, 0 to 65535";
  const std::vector<std::pair<std::vector<std::string_view>, std::string>> cases {
    {{"--auto-interval"}, "--auto-interval needs a value"},
    {{"--auto-interval", "300"}, interval_error},
    {{"--auto-interval", "300-100"}, interval_error},
    {{"--auto-interval", "10-100"}, interval_error},
    {{"--auto-interval", "100-x"}, interval_error},
    {{"--time-port"}, "--time-port needs a value"},
    {{"--time-port", "70000"}, port_error},
    {{"--time-port", "48100.5"}, port_error},
  };
  for (const auto &[args, expected] : cases) {
    std::string error;
    EXPECT_FALSE(parse(args, error));
    EXPECT_EQ(error, expected);
  }
}

TEST(LatencyTestPatternTest, FramesLogPathSitsNextToTheLog) {
  EXPECT_EQ(latency_test::frames_log_path("C:\\temp\\run.csv"), "C:\\temp\\run.frames.csv");
  EXPECT_EQ(latency_test::frames_log_path("run.log"), "run.log.frames.csv");
  EXPECT_EQ(latency_test::frames_log_path(".csv"), ".frames.csv");
  EXPECT_EQ(latency_test::frames_log_path("x"), "x.frames.csv");
}

TEST(LatencyTestPatternTest, ParseOptionsReadsUploadSettings) {
  std::string error;
  const auto options = parse({"--results", "D:\\r", "--analyzer", "C:\\a\\analyze_video.py", "--python", "C:\\py\\python.exe"}, error);
  ASSERT_TRUE(options) << error;
  EXPECT_EQ(options->results_dir, "D:\\r");
  EXPECT_EQ(options->analyzer_path, "C:\\a\\analyze_video.py");
  EXPECT_EQ(options->python_path, "C:\\py\\python.exe");
  EXPECT_TRUE(parse({}, error)->results_dir.empty());
  EXPECT_FALSE(parse({"--results"}, error));
  EXPECT_EQ(error, "--results needs a value");
}

TEST(LatencyTestPatternTest, StatusTextDescribesPresentation) {
  latency_test::status_t status;
  status.fps = 120;
  status.refresh_hz = 120;
  status.tearing = true;
  status.measured_fps = 119.84;
  status.auto_flip = true;
  status.auto_min_ms = 150;
  status.auto_max_ms = 400;
  status.latency_cam_port = 48100;
  EXPECT_EQ(latency_test::status_text(status),
            "120 fps (the display's refresh) \xC2\xB7 display 120 Hz \xC2\xB7 v-sync off, tearing\n"
            "presenting 119.8 fps \xC2\xB7 auto flips every 150\xE2\x80\x93" "400 ms \xC2\xB7 Latency Cam port 48100");

  status.fps = 117.5;
  status.fps_from_display = false;
  status.refresh_hz = 0;
  status.vsync = true;
  status.measured_fps = 0;
  status.auto_flip = false;
  status.latency_cam_port = 0;
  EXPECT_EQ(latency_test::status_text(status),
            "117.5 fps (--fps) \xC2\xB7 display rate unknown \xC2\xB7 v-sync on\n"
            "presenting \xE2\x80\xA6 \xC2\xB7 flips on key presses \xC2\xB7 Latency Cam off");

  status.vsync = false;
  status.tearing = false;
  EXPECT_NE(latency_test::status_text(status).find("v-sync off (no tearing support)"), std::string::npos);
}
