/**
 * @file app/pattern.h
 * @brief Platform independent parts of the latency test app: options, Gray codes and the test pattern layout.
 *
 * The test pattern is built to be read by a camera pointed at the client display:
 *  - A large trigger patch in the middle of the screen flips between black and white on every input.
 *  - A row of input bits below it shows the Gray-coded input sequence number, so a camera can tell which input
 *    caused which change.
 *  - A row of frame bits above it shows the Gray-coded host frame number, so dropped or repeated frames show up.
 *  - Four bullseye fiducials in the corners let the camera find the screen and correct for perspective.
 *  - A band between the bottom fiducials shows two seven segment readouts: the host clock in milliseconds since
 *    the app started (the same timebase as the CSV log) and the milliseconds since the last input arrived, both
 *    as of when the frame was drawn.
 *  - A band between the top fiducials shows a line of text: the frame rate, how frames are presented and the
 *    rate actually presented. It is for the person running the test; the analyzer ignores it.
 *  - Everything else is noise that changes every frame, to load the encoder up to its bitrate.
 *
 * Patches sit on a block grid (64 px when the screen is big enough, otherwise 16 px) so encoder blocks do not
 * straddle patch edges, and a mid-gray quiet zone around them keeps noise from bleeding into them.
 *
 * Before the pattern the app shows a QR code for Latency Cam, on flat gray with the same four fiducials. See qr_url
 * for what it holds and place_qr for where it goes; Latency Cam relies on both.
 */
#pragma once

// standard includes
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace latency_test {
  constexpr int input_bit_count = 4;  ///< Number of patches that show the input sequence number.
  constexpr int frame_bit_count = 8;  ///< Number of patches that show the frame number.
  constexpr int clock_digit_count = 7;  ///< Digits of the host clock readout, in milliseconds.
  constexpr int since_input_digit_count = 4;  ///< Digits of the time since the last input readout, in milliseconds.
  constexpr int readout_gap_slots = 2;  ///< Empty digit slots between the two readouts.
  constexpr int readout_slot_count = clock_digit_count + readout_gap_slots + since_input_digit_count;  ///< Digit slots across the readout band.

  /**
   * @brief Distance from one digit to the next in the readout band.
   *
   * @param digit_width Width of one digit.
   * @return The digit width plus the gap after it.
   */
  constexpr int readout_advance(int digit_width) {
    return digit_width + digit_width / 4;
  }

  /**
   * @brief An axis aligned rectangle in pixels.
   */
  struct rect_t {
    int x;  ///< Left edge.
    int y;  ///< Top edge.
    int w;  ///< Width.
    int h;  ///< Height.

    /**
     * @brief Check whether a pixel lies inside the rectangle.
     *
     * @param px Pixel column.
     * @param py Pixel row.
     * @return True when the pixel is inside.
     */
    [[nodiscard]] constexpr bool contains(int px, int py) const {
      return px >= x && px < x + w && py >= y && py < y + h;
    }

    /**
     * @brief Compare two rectangles.
     *
     * @return True when all edges match.
     */
    constexpr bool operator==(const rect_t &) const = default;
  };

  /**
   * @brief Where every element of the test pattern is drawn.
   */
  struct layout_t {
    int grid;  ///< Block grid that patch edges are aligned to.
    int cell;  ///< Base patch size.
    rect_t trigger;  ///< Patch that flips on every input.
    rect_t quiet;  ///< Mid-gray zone around the trigger and bit rows; no noise is drawn here.
    std::array<rect_t, 4> fiducials;  ///< Corner bullseyes: top left, top right, bottom left, bottom right.
    std::array<rect_t, frame_bit_count> frame_bits;  ///< Frame number bits, most significant first.
    std::array<rect_t, input_bit_count> input_bits;  ///< Input sequence bits, most significant first.
    rect_t readout;  ///< Black band between the bottom fiducials holding the clock and time since input digits.
    rect_t info;  ///< Black band between the top fiducials with a text line on the frame rate and presentation.
    int digit_x;  ///< Left edge of the first digit.
    int digit_y;  ///< Top edge of the digits.
    int digit_w;  ///< Width of one seven segment digit; its height is twice this.
  };

  /**
   * @brief Convert a number to its reflected binary Gray code.
   *
   * Consecutive values differ in exactly one bit, so a camera frame caught mid-change reads as one of the two values
   * instead of an unrelated one.
   *
   * @param value Value to convert.
   * @return Gray code of the value.
   */
  constexpr std::uint32_t gray_encode(std::uint32_t value) {
    return value ^ (value >> 1);
  }

  /**
   * @brief Convert a reflected binary Gray code back to a number.
   *
   * @param gray Gray code to convert.
   * @return The value it encodes.
   */
  constexpr std::uint32_t gray_decode(std::uint32_t gray) {
    for (std::uint32_t shift = 1; shift < 32; shift <<= 1) {
      gray ^= gray >> shift;
    }
    return gray;
  }

  /**
   * @brief Round a coordinate down to a multiple of the grid.
   *
   * @param value Coordinate to round.
   * @param grid Grid size.
   * @return The rounded coordinate.
   */
  constexpr int align_down(int value, int grid) {
    return value - value % grid;
  }

  /**
   * @brief Lay out the test pattern for a screen size.
   *
   * @param width Screen width in pixels.
   * @param height Screen height in pixels.
   * @return The layout.
   */
  constexpr layout_t compute_layout(int width, int height) {
    layout_t layout {};

    // The pattern needs about 5x5 cells around the middle, so an eighth of the short side leaves room for fiducials.
    // Cells are a multiple of 32 so half cells (the frame bits) still sit on the 16 px grid.
    const int raw_cell = std::min(width, height) / 8;
    layout.grid = raw_cell >= 64 ? 64 : 16;
    layout.cell = std::max(align_down(raw_cell, std::max(layout.grid, 32)), 32);
    const int grid = layout.grid;
    const int cell = layout.cell;
    const int half = cell / 2;

    // The trigger stays in the middle so its scanout position is the same on every display.
    layout.trigger = {align_down(width / 2 - cell, grid), align_down(height / 2 - cell, grid), 2 * cell, 2 * cell};

    // Both bit rows are four cells wide and centered on the trigger.
    const int row_x = layout.trigger.x - cell;
    const int frame_row_y = layout.trigger.y - cell;
    const int input_row_y = layout.trigger.y + layout.trigger.h + half;
    for (int i = 0; i < frame_bit_count; ++i) {
      layout.frame_bits[i] = {row_x + i * half, frame_row_y, half, half};
    }
    for (int i = 0; i < input_bit_count; ++i) {
      layout.input_bits[i] = {row_x + i * cell, input_row_y, cell, half};
    }

    const int quiet_top = frame_row_y - half;
    const int quiet_bottom = input_row_y + half + half;
    layout.quiet = {row_x - half, quiet_top, 4 * cell + cell, quiet_bottom - quiet_top};

    const int right = align_down(width - grid - cell, grid);
    const int bottom = align_down(height - grid - cell, grid);
    layout.fiducials = {
      rect_t {grid, grid, cell, cell},
      rect_t {right, grid, cell, cell},
      rect_t {grid, bottom, cell, cell},
      rect_t {right, bottom, cell, cell},
    };

    // The readout fills the space between the bottom fiducials, with digits as large as fit.
    const int readout_x = 2 * grid + cell;
    layout.readout = {readout_x, bottom, right - grid - readout_x, cell};
    // The status text sits in the same place between the top fiducials
    // and stops short of the quiet zone, so on short screens it is thinner or empty
    layout.info = {readout_x, grid, right - grid - readout_x, std::clamp(quiet_top - grid, 0, cell)};
    const int fit_width = layout.readout.w * 4 / (5 * readout_slot_count + 2);
    const int fit_height = layout.readout.h * 3 / 8;
    layout.digit_w = std::max(align_down(std::min(fit_width, fit_height), 4), 4);
    const int digits_width = readout_slot_count * readout_advance(layout.digit_w) - layout.digit_w / 4;
    layout.digit_x = layout.readout.x + (layout.readout.w - digits_width) / 2;
    layout.digit_y = layout.readout.y + (layout.readout.h - 2 * layout.digit_w) / 2;

    return layout;
  }

  /**
   * @brief Check whether a patch showing one bit of a Gray-coded value is lit.
   *
   * The row shows the Gray code of the value modulo the row size, not the low bits of the value's Gray code: the
   * low bits of a reflected Gray code count back down after every wrap, which reads as going backwards.
   *
   * @param value Value being shown (not yet Gray coded).
   * @param bit_count Number of patches in the row.
   * @param index Patch index, 0 being the leftmost and most significant.
   * @return True when the patch is white.
   */
  constexpr bool bit_lit(std::uint32_t value, int bit_count, int index) {
    const std::uint32_t wrapped = value & ((1u << bit_count) - 1);
    return (gray_encode(wrapped) >> (bit_count - 1 - index)) & 1;
  }

  /**
   * @brief Command line options of the test app.
   */
  struct options_t {
    bool windowed = false;  ///< Run in a 1280x720 window instead of borderless fullscreen.
    bool vsync = false;  ///< Wait for vertical blank on present instead of tearing.
    bool help = false;  ///< Show usage and exit.
    double noise = 0.5;  ///< Background noise amplitude, 0 (flat gray) to 1 (full range).
    double fps = 0;  ///< Background update rate; 0 matches the display refresh rate.
    std::string log_path;  ///< CSV log path; empty picks a file in the temp directory.
    bool qr = true;  ///< Start on the QR code for Latency Cam instead of the test pattern.
    bool auto_flip = true;  ///< Flip the trigger on its own at random intervals.
    double auto_min_ms = 150;  ///< Shortest interval between automatic flips.
    double auto_max_ms = 400;  ///< Longest interval between automatic flips.
    int time_port = 48100;  ///< UDP port of the clock sync server and TCP port of the upload server; 0 turns both off.
    std::string results_dir;  ///< Where Latency Cam uploads and their results go; empty picks a results folder next to the app.
    std::string analyzer_path;  ///< analyze_video.py; empty looks next to the executable.
    std::string python_path;  ///< Python interpreter; empty finds python.exe or py.exe on PATH.
  };

  /**
   * @brief Usage text for the test app.
   */
  constexpr std::string_view usage =
    "Usage: latency-test [options]\n"
    "\n"
    "  --windowed       Run in a 1280x720 window instead of borderless fullscreen.\n"
    "  --vsync          Wait for vertical blank instead of presenting immediately with tearing.\n"
    "  --noise <0..1>   Background noise amplitude (default 0.5, 0 disables the noise).\n"
    "  --no-noise       Flat gray background instead of noise, so the encoder is not pushed\n"
    "                   to its bitrate (same as --noise 0).\n"
    "  --fps <n>        Background update rate (default: display refresh rate).\n"
    "  --log <path>     CSV log of inputs and the frames that answered them\n"
    "                   (default: %TEMP%\\latency_test.csv). Every frame's\n"
    "                   present time goes to the same name ending .frames.csv.\n"
    "  --no-qr          Start on the test pattern instead of the QR code for Latency Cam.\n"
    "  --no-auto        Start without automatic flips. By default the trigger flips on\n"
    "                   its own every 150 to 400 ms, at random, on the next scheduled\n"
    "                   frame (key presses still get an extra frame). A toggles it.\n"
    "  --auto-interval <min>-<max>\n"
    "                   Interval range for automatic flips in ms, e.g. 100-300.\n"
    "  --time-port <n>  Port answering Latency Cam: clock sync on UDP and uploads on TCP\n"
    "                   (default 48100).\n"
    "  --no-time-server Do not answer clock sync requests or accept uploads.\n"
    "  --results <dir>  Where uploaded recordings and their analysis go (default: a\n"
    "                   results folder next to this app, or Documents\\Latency Cam\n"
    "                   when that can't be written).\n"
    "  --analyzer <path> latency-analyzer.exe or analyze_video.py to run on uploads\n"
    "                   (default: next to this app, preferring the exe).\n"
    "  --python <path>  Python to run it with (default: python.exe or py.exe on PATH).\n"
    "  --help           Show this text.\n"
    "\n"
    "Any key press (except Esc, A, F and V) or mouse button press flips the trigger\n"
    "patch, as do the automatic flips. A toggles automatic flips, V toggles v-sync, and\n"
    "F types a new frame rate (Enter applies, empty for the display's refresh rate).\n"
    "The band at the bottom shows the host clock and the time since the last input, in ms.\n"
    "Close with Esc, Alt+F4 or by ending the stream.\n";

  /**
   * @brief Parse a number argument.
   *
   * @param text Argument text.
   * @param min Smallest allowed value.
   * @param max Largest allowed value.
   * @return The value, or nothing when the text is not a number in range.
   */
  inline std::optional<double> parse_number(std::string_view text, double min, double max) {
    double value {};
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc {} || end != text.data() + text.size() || value < min || value > max) {
      return std::nullopt;
    }
    return value;
  }

  /**
   * @brief Parse the command line.
   *
   * @param args Arguments without the program name.
   * @param error Set to a description of the problem when parsing fails.
   * @return The options, or nothing when the command line is invalid.
   */
  inline std::optional<options_t> parse_options(const std::vector<std::string_view> &args, std::string &error) {
    options_t options;
    for (std::size_t i = 0; i < args.size(); ++i) {
      const std::string_view arg = args[i];
      const auto value = [&]() -> std::optional<std::string_view> {
        if (i + 1 >= args.size()) {
          error = std::string(arg) + " needs a value";
          return std::nullopt;
        }
        return args[++i];
      };

      if (arg == "--windowed") {
        options.windowed = true;
      }
      else if (arg == "--vsync") {
        options.vsync = true;
      }
      else if (arg == "--no-noise") {
        options.noise = 0;
      }
      else if (arg == "--auto") {
        // The default now; still accepted so older command lines work
        options.auto_flip = true;
      }
      else if (arg == "--no-auto") {
        options.auto_flip = false;
      }
      else if (arg == "--no-qr") {
        options.qr = false;
      }
      else if (arg == "--no-time-server") {
        options.time_port = 0;
      }
      else if (arg == "--auto-interval") {
        const auto text = value();
        if (!text) {
          return std::nullopt;
        }
        const auto dash = text->find('-');
        const auto low = dash == std::string_view::npos ? std::nullopt : parse_number(text->substr(0, dash), 20, 10000);
        const auto high = dash == std::string_view::npos ? std::nullopt : parse_number(text->substr(dash + 1), 20, 10000);
        if (!low || !high || *low > *high) {
          error = "--auto-interval must be <min>-<max> in ms, between 20 and 10000";
          return std::nullopt;
        }
        options.auto_min_ms = *low;
        options.auto_max_ms = *high;
      }
      else if (arg == "--time-port") {
        const auto text = value();
        if (!text) {
          return std::nullopt;
        }
        const auto number = parse_number(*text, 0, 65535);
        if (!number || *number != static_cast<int>(*number)) {
          error = "--time-port must be a port number, 0 to 65535";
          return std::nullopt;
        }
        options.time_port = static_cast<int>(*number);
      }
      else if (arg == "--help" || arg == "-h" || arg == "/?") {
        options.help = true;
      }
      else if (arg == "--noise" || arg == "--fps") {
        const auto text = value();
        if (!text) {
          return std::nullopt;
        }
        const bool is_noise = arg == "--noise";
        const auto number = is_noise ? parse_number(*text, 0, 1) : parse_number(*text, 1, 1000);
        if (!number) {
          error = std::string(arg) + (is_noise ? " must be between 0 and 1" : " must be between 1 and 1000");
          return std::nullopt;
        }
        (is_noise ? options.noise : options.fps) = *number;
      }
      else if (arg == "--log" || arg == "--results" || arg == "--analyzer" || arg == "--python") {
        const auto text = value();
        if (!text) {
          return std::nullopt;
        }
        auto &target = arg == "--log"     ? options.log_path :
                       arg == "--results" ? options.results_dir :
                       arg == "--analyzer" ? options.analyzer_path :
                                             options.python_path;
        target = *text;
      }
      else {
        error = "Unknown option " + std::string(arg);
        return std::nullopt;
      }
    }
    return options;
  }

  constexpr int qr_quiet_modules = 4;  ///< Blank modules around a QR code, as the standard requires.
  constexpr int qr_max_hosts = 3;  ///< Most addresses listed in the QR code, to keep its modules large.
  constexpr std::string_view qr_link = "https://moreorlesssoftware.github.io/c";  ///< Opens Latency Cam, or its download page.

  /**
   * @brief An IPv4 address of one of the host's network adapters.
   */
  struct ipv4_candidate_t {
    std::array<std::uint8_t, 4> address;  ///< The address, most significant byte first.
    bool has_gateway;  ///< Whether its adapter has a default gateway, which virtual adapters usually don't.
  };

  /**
   * @brief Check whether an address is in one of the private IPv4 ranges.
   *
   * @param address The address.
   * @return True for 10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16.
   */
  constexpr bool is_private_ipv4(const std::array<std::uint8_t, 4> &address) {
    return address[0] == 10 ||
           (address[0] == 172 && address[1] >= 16 && address[1] <= 31) ||
           (address[0] == 192 && address[1] == 168);
  }

  /**
   * @brief Choose the addresses to list in the QR code.
   *
   * @param candidates The host's addresses.
   * @return Up to qr_max_hosts private addresses as text, those on adapters with a gateway first.
   */
  inline std::vector<std::string> qr_hosts(std::vector<ipv4_candidate_t> candidates) {
    std::stable_partition(candidates.begin(), candidates.end(), [](const ipv4_candidate_t &candidate) {
      return candidate.has_gateway;
    });
    std::vector<std::string> hosts;
    for (const auto &candidate : candidates) {
      if (is_private_ipv4(candidate.address) && hosts.size() < qr_max_hosts) {
        const auto &a = candidate.address;
        hosts.push_back(std::to_string(a[0]) + '.' + std::to_string(a[1]) + '.' + std::to_string(a[2]) + '.' + std::to_string(a[3]));
      }
    }
    return hosts;
  }

  /**
   * @brief Build the link shown in the QR code.
   *
   * The details are in the fragment, which browsers don't send, so LAN addresses stay out of the web server's logs.
   * With no addresses, Latency Cam finds the host by broadcast as usual.
   *
   * @param hosts Addresses Latency Cam can try.
   * @param port Port of the clock sync and upload servers.
   * @param width Screen width in pixels.
   * @param height Screen height in pixels.
   * @return The link, like https://example/c#h=10.0.0.5,192.168.1.2&p=48100&s=1920x1080.
   */
  inline std::string qr_url(const std::vector<std::string> &hosts, int port, int width, int height) {
    std::string url = std::string(qr_link) + "#h=";
    for (std::size_t i = 0; i < hosts.size(); ++i) {
      url += (i ? "," : "") + hosts[i];
    }
    return url + "&p=" + std::to_string(port) + "&s=" + std::to_string(width) + 'x' + std::to_string(height);
  }

  /**
   * @brief Where a QR code goes on the screen.
   */
  struct qr_placement_t {
    int module_px;  ///< Side of one module in pixels.
    rect_t rect;  ///< The code including its quiet zone.
  };

  /**
   * @brief Work out where a QR code goes: centered, in whole pixels per module, about half of the screen's short side.
   *
   * Latency Cam repeats this calculation to work out from the code it sees where the corners of the screen are, so
   * changing it means changing Latency Cam too.
   *
   * @param width Screen width in pixels.
   * @param height Screen height in pixels.
   * @param size Modules along one side of the code, without the quiet zone.
   * @return The placement.
   */
  constexpr qr_placement_t place_qr(int width, int height, int size) {
    const int total = size + 2 * qr_quiet_modules;
    const int module_px = std::max(std::min(width, height) / 2 / total, 1);
    const int side = total * module_px;
    return {module_px, {(width - side) / 2, (height - side) / 2, side, side}};
  }

  /**
   * @brief Draw the QR screen: flat gray, the pattern's fiducials and the code.
   *
   * @param width Screen width in pixels.
   * @param height Screen height in pixels.
   * @param size Modules along one side of the code.
   * @param dark Whether the module at a column and row is dark.
   * @return Pixels as 0xAARRGGBB, row by row.
   */
  inline std::vector<std::uint32_t> qr_screen_pixels(int width, int height, int size, const std::function<bool(int, int)> &dark) {
    constexpr std::uint32_t gray = 0xFF808080;
    constexpr std::uint32_t black = 0xFF000000;
    constexpr std::uint32_t white = 0xFFFFFFFF;
    std::vector<std::uint32_t> pixels(static_cast<std::size_t>(width) * height, gray);
    const auto at = [&](int x, int y) -> std::uint32_t & {
      return pixels[static_cast<std::size_t>(y) * width + x];
    };

    // The same square bullseyes as the pixel shader: white center, black ring, white outer ring
    for (const rect_t &r : compute_layout(width, height).fiducials) {
      for (int y = std::max(r.y, 0); y < std::min(r.y + r.h, height); ++y) {
        for (int x = std::max(r.x, 0); x < std::min(r.x + r.w, width); ++x) {
          const int ring = std::max(std::abs(x - r.x - r.w / 2), std::abs(y - r.y - r.h / 2)) * 8 / r.w;
          at(x, y) = ring == 0 || ring >= 3 ? white : black;
        }
      }
    }

    const auto [module_px, rect] = place_qr(width, height, size);
    for (int y = std::max(rect.y, 0); y < std::min(rect.y + rect.h, height); ++y) {
      for (int x = std::max(rect.x, 0); x < std::min(rect.x + rect.w, width); ++x) {
        const int column = (x - rect.x) / module_px - qr_quiet_modules;
        const int row = (y - rect.y) / module_px - qr_quiet_modules;
        at(x, y) = column >= 0 && row >= 0 && column < size && row < size && dark(column, row) ? black : white;
      }
    }
    return pixels;
  }

  /**
   * @brief Where the background frame rate came from.
   */
  enum class fps_source_e {
    display,  ///< The display's refresh rate.
    rtss,  ///< RivaTuner Statistics Server's frame rate limit.
    option,  ///< --fps, or typed after F.
  };

  /**
   * @brief The name of a frame rate source, as the logs write it.
   *
   * @param source The source.
   * @return "display", "rtss" or "option".
   */
  inline const char *fps_source_name(fps_source_e source) {
    switch (source) {
      case fps_source_e::display:
        return "display";
      case fps_source_e::rtss:
        return "rtss";
      default:
        return "option";
    }
  }

  /**
   * @brief The rate the app's own frame timer runs at.
   *
   * Under RTSS's limit the timer runs 5% faster, so RTSS alone sets the pace, as it would for a game. With both aiming
   * for the same rate, either could make a frame wait, and the times the app calls Present would jitter for no reason
   * the client could see.
   *
   * @param fps The frame rate the app aims for.
   * @param source Where it came from.
   * @return The timer's rate in fps.
   */
  inline double timer_fps(double fps, fps_source_e source) {
    return source == fps_source_e::rtss ? fps * 1.05 : fps;
  }

  /**
   * @brief Read the frame rate limit from one RivaTuner Statistics Server profile.
   *
   * Profiles are INI files; the limit is Limit / LimitDenominator in the [Framerate] section, where 0 means no limit.
   * A frame time limit (LimitTime) is not read.
   *
   * @param profile The profile file's text.
   * @return The limit in fps, 0 when the profile sets no limit, or nothing when the profile has no Limit, so another
   * profile decides.
   */
  inline std::optional<double> rtss_profile_limit(std::string_view profile) {
    std::string_view section;
    std::optional<double> limit;
    double denominator = 1;
    while (!profile.empty()) {
      const auto end = profile.find('\n');
      std::string_view line = profile.substr(0, end);
      profile = end == std::string_view::npos ? std::string_view {} : profile.substr(end + 1);
      while (!line.empty() && (line.back() == '\r' || line.back() == ' ' || line.back() == '\t')) {
        line.remove_suffix(1);
      }
      while (!line.empty() && (line.front() == ' ' || line.front() == '\t')) {
        line.remove_prefix(1);
      }
      if (line.size() >= 2 && line.front() == '[' && line.back() == ']') {
        section = line.substr(1, line.size() - 2);
        continue;
      }
      const auto equals = line.find('=');
      if (section != "Framerate" || equals == std::string_view::npos) {
        continue;
      }
      const std::string_view key = line.substr(0, equals);
      const std::string_view value = line.substr(equals + 1);
      double number = 0;
      if (std::from_chars(value.data(), value.data() + value.size(), number).ec != std::errc {}) {
        continue;
      }
      if (key == "Limit") {
        limit = number;
      }
      else if (key == "LimitDenominator" && number > 0) {
        denominator = number;
      }
    }
    if (!limit) {
      return std::nullopt;
    }
    return *limit > 0 ? *limit / denominator : 0.0;
  }

  /**
   * @brief The frame rate limit RivaTuner Statistics Server applies to an app: its own profile's, else the global one's.
   *
   * @param app_profile The app's profile file's text, if it has one.
   * @param global_profile The global profile file's text, if it could be read.
   * @return The limit in fps, or nothing when there is none.
   */
  inline std::optional<double> rtss_limit(std::optional<std::string_view> app_profile, std::optional<std::string_view> global_profile) {
    std::optional<double> limit = app_profile ? rtss_profile_limit(*app_profile) : std::nullopt;
    if (!limit && global_profile) {
      limit = rtss_profile_limit(*global_profile);
    }
    if (!limit || *limit <= 0) {
      return std::nullopt;
    }
    return limit;
  }

  /**
   * @brief What the status line in the info band describes.
   */
  struct status_t {
    double fps = 0;  ///< Background frame rate in use.
    fps_source_e fps_source = fps_source_e::display;  ///< Where fps came from.
    fps_source_e default_source = fps_source_e::display;  ///< Where the frame rate comes from when none is set.
    double present_wait_ms = 0;  ///< Mean time Present held the app over the last second.
    int refresh_hz = 0;  ///< Display refresh rate; 0 when unknown.
    bool vsync = false;  ///< Whether presents wait for vertical blank.
    bool tearing = false;  ///< Whether presents may tear (only without v-sync).
    double measured_fps = 0;  ///< Frames actually presented over the last second; 0 before the first second.
    bool auto_flip = false;  ///< Whether the trigger flips on its own.
    double auto_min_ms = 0;  ///< Shortest automatic flip interval.
    double auto_max_ms = 0;  ///< Longest automatic flip interval.
    int latency_cam_port = 0;  ///< Port Latency Cam can use; 0 when the servers are off or failed.
    bool entering_fps = false;  ///< Whether a new frame rate is being typed.
    std::string fps_entry;  ///< The frame rate typed so far.
    bool fps_entry_invalid = false;  ///< Whether the last Enter was refused as out of range.
    std::string activity;  ///< The upload or analysis in progress; empty when there is none.
    int activity_percent = -1;  ///< Its progress from 0 to 100, or -1 when it can't be told.
  };

  /**
   * @brief Typing a new frame rate in the app: F starts, digits and '.' type, Backspace deletes, Enter applies and
   * Esc cancels. Enter with nothing typed goes back to the display's refresh rate.
   */
  struct fps_entry_t {
    /**
     * @brief What a key did.
     */
    enum class result_e {
      typing,  ///< The entry carries on.
      apply,  ///< A frame rate was entered.
      cancel,  ///< The entry was cancelled.
    };

    static constexpr unsigned key_back = 0x08;  ///< VK_BACK
    static constexpr unsigned key_return = 0x0D;  ///< VK_RETURN
    static constexpr unsigned key_escape = 0x1B;  ///< VK_ESCAPE
    static constexpr unsigned key_numpad0 = 0x60;  ///< VK_NUMPAD0, followed by the other digits
    static constexpr unsigned key_decimal = 0x6E;  ///< VK_DECIMAL
    static constexpr unsigned key_period = 0xBE;  ///< VK_OEM_PERIOD
    static constexpr std::size_t max_length = 7;  ///< Longest entry, e.g. 1000.00

    bool active = false;  ///< Whether a frame rate is being typed.
    bool invalid = false;  ///< Whether the last Enter was refused.
    std::string text;  ///< What has been typed.

    /**
     * @brief Start typing a new frame rate.
     */
    void start() {
      active = true;
      invalid = false;
      text.clear();
    }

    /**
     * @brief Handle a key press while typing.
     *
     * @param vkey Windows virtual key code.
     * @param fps Set to the frame rate entered when the result is apply; 0 means the display's refresh rate.
     * @return What the key did.
     */
    result_e key(unsigned vkey, double &fps) {
      char typed = 0;
      if (vkey >= '0' && vkey <= '9') {
        typed = static_cast<char>(vkey);
      }
      else if (vkey >= key_numpad0 && vkey <= key_numpad0 + 9) {
        typed = static_cast<char>('0' + (vkey - key_numpad0));
      }
      else if ((vkey == key_period || vkey == key_decimal) && text.find('.') == std::string::npos) {
        typed = '.';
      }
      if (typed) {
        if (text.size() < max_length) {
          text += typed;
        }
        invalid = false;
        return result_e::typing;
      }
      if (vkey == key_back) {
        if (!text.empty()) {
          text.pop_back();
        }
        invalid = false;
        return result_e::typing;
      }
      if (vkey == key_escape) {
        active = false;
        return result_e::cancel;
      }
      if (vkey == key_return) {
        if (text.empty()) {
          fps = 0;
          active = false;
          return result_e::apply;
        }
        const auto value = parse_number(text, 1, 1000);
        if (!value) {
          invalid = true;
          return result_e::typing;
        }
        fps = *value;
        active = false;
        return result_e::apply;
      }
      // Other keys are ignored while typing, so they don't flip the trigger
      return result_e::typing;
    }
  };

  /**
   * @brief The two lines of the status text: how frames are made and presented, then what is happening.
   *
   * @param status What to describe.
   * @return The text, with the lines separated by a newline.
   */
  inline std::string status_text(const status_t &status) {
    const auto number = [](double value) {
      char buffer[32];
      const auto [end, error] = std::to_chars(buffer, buffer + sizeof(buffer), value, std::chars_format::fixed, value == static_cast<int>(value) ? 0 : 1);
      return std::string(buffer, end);
    };
    const auto source = [](fps_source_e source) {
      return source == fps_source_e::display ? "the display's refresh" : source == fps_source_e::rtss ? "RTSS's limit" : "";
    };
    std::string text = number(status.fps) + " fps" + (status.fps_source == fps_source_e::option ? std::string(" (F sets)") : std::string(" (") + source(status.fps_source) + ", F sets)");
    text += status.refresh_hz > 0 ? " \xC2\xB7 display " + std::to_string(status.refresh_hz) + " Hz" : " \xC2\xB7 display rate unknown";
    if (status.vsync) {
      text += status.fps_source == fps_source_e::display ? " \xC2\xB7 v-sync on, paced by the display" : " \xC2\xB7 v-sync on";
    }
    else {
      text += status.tearing ? " \xC2\xB7 v-sync off, tearing" : " \xC2\xB7 v-sync off (no tearing support)";
    }
    text += " (V toggles)\n";
    if (status.entering_fps) {
      // The second line becomes the prompt while a frame rate is typed
      text += "New frame rate: " + status.fps_entry + "_";
      text += status.fps_entry_invalid ? " \xC2\xB7 must be 1 to 1000" : std::string(" \xC2\xB7 Enter applies (empty: ") + source(status.default_source) + ")";
      text += " \xC2\xB7 Esc cancels";
      return text;
    }
    if (!status.activity.empty()) {
      // The second line shows an upload or analysis while there is one
      text += status.activity;
      if (status.activity_percent >= 0) {
        text += " \xC2\xB7 " + std::to_string(status.activity_percent) + "%";
      }
      return text;
    }
    text += status.measured_fps > 0 ? "presenting " + number(status.measured_fps) + " fps, " + number(std::round(status.present_wait_ms * 10) / 10) + " ms in Present" : "presenting \xE2\x80\xA6";
    text += status.auto_flip ? " \xC2\xB7 auto flips every " + number(status.auto_min_ms) + "\xE2\x80\x93" + number(status.auto_max_ms) + " ms" : " \xC2\xB7 auto flips off";
    text += " (A toggles)";
    text += status.latency_cam_port > 0 ? " \xC2\xB7 Latency Cam port " + std::to_string(status.latency_cam_port) : " \xC2\xB7 Latency Cam off";
    return text;
  }

  /**
   * @brief Path of the per-frame log that goes with an input log.
   *
   * @param log_path Path of the input log.
   * @return The same path with .csv replaced by .frames.csv, or .frames.csv appended.
   */
  inline std::string frames_log_path(std::string_view log_path) {
    constexpr std::string_view extension = ".csv";
    if (log_path.size() >= extension.size() && log_path.substr(log_path.size() - extension.size()) == extension) {
      return std::string(log_path.substr(0, log_path.size() - extension.size())) + ".frames.csv";
    }
    return std::string(log_path) + ".frames.csv";
  }
}  // namespace latency_test
