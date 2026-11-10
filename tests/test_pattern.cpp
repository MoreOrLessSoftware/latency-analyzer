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
  EXPECT_TRUE(options->auto_flip);
  EXPECT_DOUBLE_EQ(options->auto_min_ms, 150);
  EXPECT_DOUBLE_EQ(options->auto_max_ms, 400);
  EXPECT_EQ(options->time_port, 48100);

  EXPECT_FALSE(parse({"--no-auto"}, error)->auto_flip);
  EXPECT_TRUE(parse({"--no-auto", "--auto"}, error)->auto_flip);
  // The interval can be set while starting with automatic flips off, for Space to turn on later
  options = parse({"--no-auto", "--auto-interval", "50-60"}, error);
  ASSERT_TRUE(options) << error;
  EXPECT_FALSE(options->auto_flip);
  EXPECT_DOUBLE_EQ(options->auto_min_ms, 50);
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

TEST(LatencyTestPatternTest, StatusTextShowsAnUploadOrAnalysisInProgress) {
  latency_test::status_t status;
  status.fps = 60;
  status.activity = "Analysing run: reading frames, 57 of 100";
  status.activity_percent = 54;
  std::string text = latency_test::status_text(status);
  EXPECT_NE(text.find("\nAnalysing run: reading frames, 57 of 100 \xC2\xB7 54%"), std::string::npos);
  EXPECT_EQ(text.find("auto flips"), std::string::npos);

  // Without a percentage there's nothing to add
  status.activity_percent = -1;
  EXPECT_TRUE(latency_test::status_text(status).ends_with("\nAnalysing run: reading frames, 57 of 100"));

  // Typing a frame rate takes priority, so the prompt stays visible
  status.entering_fps = true;
  EXPECT_NE(latency_test::status_text(status).find("\nNew frame rate: "), std::string::npos);

  // And the usual line comes back when it's done
  status.entering_fps = false;
  status.activity.clear();
  EXPECT_NE(latency_test::status_text(status).find("auto flips"), std::string::npos);
}

TEST(LatencyTestPatternTest, ParseOptionsReadsQr) {
  std::string error;
  EXPECT_TRUE(parse({}, error)->qr);
  EXPECT_FALSE(parse({"--no-qr"}, error)->qr);
}

TEST(LatencyTestPatternTest, QrHostsPreferGatewaysAndKeepPrivateAddresses) {
  const auto hosts = latency_test::qr_hosts({
    {{172, 24, 0, 1}, false},  // A virtual adapter
    {{8, 8, 8, 8}, true},  // Public
    {{192, 168, 1, 20}, true},
    {{169, 254, 3, 4}, false},  // Link local
    {{100, 64, 0, 1}, true},  // Carrier grade NAT, as VPNs use
    {{172, 32, 0, 1}, true},  // Just past the private range
    {{10, 0, 0, 5}, true},
  });
  EXPECT_EQ(hosts, (std::vector<std::string> {"192.168.1.20", "10.0.0.5", "172.24.0.1"}));
}

TEST(LatencyTestPatternTest, QrHostsAreLimited) {
  const auto hosts = latency_test::qr_hosts({
    {{10, 0, 0, 1}, true},
    {{10, 0, 0, 2}, true},
    {{10, 0, 0, 3}, true},
    {{10, 0, 0, 4}, true},
  });
  EXPECT_EQ(hosts.size(), latency_test::qr_max_hosts);
  EXPECT_EQ(hosts.back(), "10.0.0.3");
}

TEST(LatencyTestPatternTest, QrUrlHoldsTheConnectionDetailsInTheFragment) {
  EXPECT_EQ(latency_test::qr_url({"192.168.1.20", "10.0.0.5"}, 48100, 3840, 2160), "https://moreorlesssoftware.github.io/c#h=192.168.1.20,10.0.0.5&p=48100&s=3840x2160");
  EXPECT_EQ(latency_test::qr_url({}, 50000, 1920, 1080), "https://moreorlesssoftware.github.io/c#h=&p=50000&s=1920x1080");
}

TEST(LatencyTestPatternTest, QrIsCenteredInWholePixelsAtHalfTheHeight) {
  const auto [module_px, rect] = latency_test::place_qr(1920, 1080, 37);
  EXPECT_EQ(module_px, 12);
  EXPECT_EQ(rect, (rect_t {690, 270, 540, 540}));
  // Rounds down rather than growing past half
  EXPECT_EQ(latency_test::place_qr(1920, 1200, 37).module_px, 13);
  // Never vanishes in a tiny window
  EXPECT_EQ(latency_test::place_qr(40, 30, 37).module_px, 1);
}

TEST(LatencyTestPatternTest, QrClearsTheFiducialsAndStatusBand) {
  for (const auto &[width, height] : std::initializer_list<std::pair<int, int>> {{1280, 720}, {1920, 1080}, {2560, 1440}, {3840, 2160}, {3440, 1440}, {1920, 1200}, {1024, 768}}) {
    const auto layout = latency_test::compute_layout(width, height);
    const rect_t rect = latency_test::place_qr(width, height, 41).rect;
    for (const rect_t &fiducial : layout.fiducials) {
      EXPECT_FALSE(overlaps(rect, fiducial)) << width << "x" << height;
    }
    EXPECT_FALSE(overlaps(rect, layout.info)) << width << "x" << height;
  }
}

TEST(LatencyTestPatternTest, QrScreenDrawsModulesQuietZoneAndFiducials) {
  constexpr int width = 1920;
  constexpr int height = 1080;
  constexpr int size = 21;
  const auto pixels = latency_test::qr_screen_pixels(width, height, size, [](int x, int y) {
    return x == 0 && y == 0;
  });
  ASSERT_EQ(pixels.size(), static_cast<std::size_t>(width) * height);
  const auto at = [&](int x, int y) {
    return pixels[static_cast<std::size_t>(y) * width + x];
  };
  constexpr std::uint32_t black = 0xFF000000;
  constexpr std::uint32_t white = 0xFFFFFFFF;
  constexpr std::uint32_t gray = 0xFF808080;

  const auto [module_px, rect] = latency_test::place_qr(width, height, size);
  const int first = latency_test::qr_quiet_modules * module_px;
  EXPECT_EQ(at(rect.x - 1, rect.y), gray);
  EXPECT_EQ(at(rect.x, rect.y), white);  // Quiet zone
  EXPECT_EQ(at(rect.x + first - 1, rect.y + first), white);
  EXPECT_EQ(at(rect.x + first, rect.y + first), black);  // The dark module, all of it
  EXPECT_EQ(at(rect.x + first + module_px - 1, rect.y + first + module_px - 1), black);
  EXPECT_EQ(at(rect.x + first + module_px, rect.y + first), white);
  EXPECT_EQ(at(rect.x + rect.w - 1, rect.y + rect.h - 1), white);

  const rect_t fiducial = latency_test::compute_layout(width, height).fiducials[0];
  EXPECT_EQ(at(fiducial.x + fiducial.w / 2, fiducial.y + fiducial.h / 2), white);
  EXPECT_EQ(at(fiducial.x + fiducial.w / 2 + fiducial.w / 4 - 1, fiducial.y + fiducial.h / 2), black);
  EXPECT_EQ(at(fiducial.x, fiducial.y), white);
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
  status.present_wait_ms = 0.04;
  status.auto_flip = true;
  status.auto_min_ms = 150;
  status.auto_max_ms = 400;
  status.latency_cam_port = 48100;
  EXPECT_EQ(latency_test::status_text(status),
            "120 fps (the display's refresh, F sets) \xC2\xB7 display 120 Hz \xC2\xB7 v-sync off, tearing (V toggles)\n"
            "presenting 119.8 fps, 0 ms in Present \xC2\xB7 auto flips every 150\xE2\x80\x93" "400 ms (A toggles) \xC2\xB7 Latency Cam port 48100");

  status.fps = 117.5;
  status.fps_source = latency_test::fps_source_e::option;
  status.refresh_hz = 0;
  status.vsync = true;
  status.measured_fps = 0;
  status.auto_flip = false;
  status.latency_cam_port = 0;
  EXPECT_EQ(latency_test::status_text(status),
            "117.5 fps (F sets) \xC2\xB7 display rate unknown \xC2\xB7 v-sync on (V toggles)\n"
            "presenting \xE2\x80\xA6 \xC2\xB7 auto flips off (A toggles) \xC2\xB7 Latency Cam off");

  // While a frame rate is typed, the second line is the prompt
  status.entering_fps = true;
  status.fps_entry = "97";
  EXPECT_NE(latency_test::status_text(status).find("\nNew frame rate: 97_ \xC2\xB7 Enter applies (empty: the display's refresh) \xC2\xB7 Esc cancels"), std::string::npos);
  status.fps_entry_invalid = true;
  EXPECT_NE(latency_test::status_text(status).find("must be 1 to 1000"), std::string::npos);
  status.entering_fps = false;
  status.fps_entry_invalid = false;

  // At the display's own rate, v-sync lets the display set the pace
  status.fps_source = latency_test::fps_source_e::display;
  EXPECT_NE(latency_test::status_text(status).find("v-sync on, paced by the display (V toggles)"), std::string::npos);

  status.vsync = false;
  status.tearing = false;
  EXPECT_NE(latency_test::status_text(status).find("v-sync off (no tearing support)"), std::string::npos);
}

TEST(LatencyTestPatternTest, StatusTextNamesRtssAndTheTimeInPresent) {
  latency_test::status_t status;
  status.fps = 97;
  status.fps_source = latency_test::fps_source_e::rtss;
  status.default_source = latency_test::fps_source_e::rtss;
  status.refresh_hz = 100;
  status.tearing = true;
  status.measured_fps = 97;
  status.present_wait_ms = 10.19;
  const std::string text = latency_test::status_text(status);
  EXPECT_EQ(text.substr(0, text.find(" \xC2\xB7")), "97 fps (RTSS's limit, F sets)");
  EXPECT_NE(text.find("\npresenting 97 fps, 10.2 ms in Present"), std::string::npos);

  // An empty entry goes back to RTSS's limit
  status.entering_fps = true;
  EXPECT_NE(latency_test::status_text(status).find("Enter applies (empty: RTSS's limit)"), std::string::npos);
}

TEST(LatencyTestPatternTest, FpsSourceNames) {
  EXPECT_STREQ(latency_test::fps_source_name(latency_test::fps_source_e::display), "display");
  EXPECT_STREQ(latency_test::fps_source_name(latency_test::fps_source_e::rtss), "rtss");
  EXPECT_STREQ(latency_test::fps_source_name(latency_test::fps_source_e::option), "option");
}

TEST(LatencyTestPatternTest, TimerRunsAheadOfRtss) {
  // Under RTSS's limit the app's own timer runs a little faster, so RTSS alone sets the pace
  EXPECT_DOUBLE_EQ(latency_test::timer_fps(97, latency_test::fps_source_e::rtss), 101.85);
  EXPECT_DOUBLE_EQ(latency_test::timer_fps(97, latency_test::fps_source_e::option), 97);
  EXPECT_DOUBLE_EQ(latency_test::timer_fps(100, latency_test::fps_source_e::display), 100);
}

TEST(LatencyTestPatternTest, ReadsRtssProfileLimits) {
  // As RTSS writes them: CRLF lines, the limit as a fraction
  const std::string global = "[OSD]\r\nLimit=5\r\n[Framerate]\r\nLimit=5986\r\nLimitDenominator=100\r\nLimitTime=0\r\n[Hooking]\r\nEnableHooking=1\r\n";
  ASSERT_TRUE(latency_test::rtss_profile_limit(global));
  EXPECT_DOUBLE_EQ(*latency_test::rtss_profile_limit(global), 59.86);
  EXPECT_DOUBLE_EQ(*latency_test::rtss_profile_limit("[Framerate]\nLimit=97\nLimitDenominator=1\n"), 97);
  // Spaces, a zero denominator and junk are tolerated
  EXPECT_DOUBLE_EQ(*latency_test::rtss_profile_limit("  [Framerate]  \n Limit=120\t\nLimitDenominator=0\nNote\nSyncLimiter=x\n"), 120);
  // No limit, and no say at all
  EXPECT_EQ(latency_test::rtss_profile_limit("[Framerate]\nLimit=0\nLimitDenominator=1"), 0.0);
  EXPECT_FALSE(latency_test::rtss_profile_limit("[OSD]\nLimit=97\n"));
  EXPECT_FALSE(latency_test::rtss_profile_limit("[Framerate]\nLimit=abc\n"));
  EXPECT_FALSE(latency_test::rtss_profile_limit(""));
}

TEST(LatencyTestPatternTest, AppProfileOverridesTheGlobalRtssLimit) {
  const std::string_view global = "[Framerate]\nLimit=97\n";
  EXPECT_EQ(latency_test::rtss_limit(std::nullopt, global), 97.0);
  EXPECT_EQ(latency_test::rtss_limit("[Framerate]\nLimit=117\n", global), 117.0);
  // An app profile with no limit turns it off for the app; one without the setting leaves it to the global profile
  EXPECT_FALSE(latency_test::rtss_limit("[Framerate]\nLimit=0\n", global));
  EXPECT_EQ(latency_test::rtss_limit("[OSD]\nEnableOSD=1\n", global), 97.0);
  EXPECT_FALSE(latency_test::rtss_limit(std::nullopt, "[Framerate]\nLimit=0\n"));
  EXPECT_FALSE(latency_test::rtss_limit(std::nullopt, std::nullopt));
}

TEST(LatencyTestPatternTest, FpsEntryTypesAppliesAndCancels) {
  using result = latency_test::fps_entry_t::result_e;
  latency_test::fps_entry_t entry;
  double fps = -1;

  entry.start();
  EXPECT_TRUE(entry.active);
  EXPECT_EQ(entry.key('9', fps), result::typing);
  EXPECT_EQ(entry.key(0x60 + 7, fps), result::typing);  // numpad 7
  EXPECT_EQ(entry.key(0xBE, fps), result::typing);  // .
  EXPECT_EQ(entry.key('5', fps), result::typing);
  EXPECT_EQ(entry.key(0x6E, fps), result::typing);  // a second decimal point is ignored
  EXPECT_EQ(entry.key('Q', fps), result::typing);  // other keys are ignored
  EXPECT_EQ(entry.text, "97.5");
  EXPECT_EQ(entry.key(0x08, fps), result::typing);  // backspace
  EXPECT_EQ(entry.text, "97.");
  EXPECT_EQ(entry.key(0x0D, fps), result::apply);
  EXPECT_DOUBLE_EQ(fps, 97);
  EXPECT_FALSE(entry.active);

  // Out of range keeps the prompt open and flags it
  entry.start();
  entry.key('2', fps);
  entry.key('0', fps);
  entry.key('0', fps);
  entry.key('0', fps);
  EXPECT_EQ(entry.key(0x0D, fps), result::typing);
  EXPECT_TRUE(entry.invalid);
  EXPECT_TRUE(entry.active);
  entry.key(0x08, fps);
  EXPECT_FALSE(entry.invalid);

  // Empty means the display's refresh rate
  entry.start();
  EXPECT_EQ(entry.key(0x0D, fps), result::apply);
  EXPECT_DOUBLE_EQ(fps, 0);

  // Esc cancels
  entry.start();
  entry.key('5', fps);
  EXPECT_EQ(entry.key(0x1B, fps), result::cancel);
  EXPECT_FALSE(entry.active);

  // The entry stops at seven characters
  entry.start();
  for (int i = 0; i < 10; ++i) {
    entry.key('1', fps);
  }
  EXPECT_EQ(entry.text, "1111111");
}
