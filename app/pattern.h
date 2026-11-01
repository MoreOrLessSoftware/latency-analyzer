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
 *  - Everything else is noise that changes every frame, to load the encoder up to its bitrate.
 *
 * Patches sit on a block grid (64 px when the screen is big enough, otherwise 16 px) so encoder blocks do not
 * straddle patch edges, and a mid-gray quiet zone around them keeps noise from bleeding into them.
 */
#pragma once

// standard includes
#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
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
    bool auto_flip = false;  ///< Flip the trigger on its own at random intervals.
    double auto_min_ms = 150;  ///< Shortest interval between automatic flips.
    double auto_max_ms = 400;  ///< Longest interval between automatic flips.
    int time_port = 48100;  ///< UDP port of the clock sync server and TCP port of the upload server; 0 turns both off.
    std::string results_dir;  ///< Where Latency Cam uploads and their results go; empty picks "Latency Cam" in Documents.
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
    "                   (default: %TEMP%\\sunshine_latency_test.csv). Every frame's\n"
    "                   present time goes to the same name ending .frames.csv.\n"
    "  --auto           Flip the trigger on its own every 150 to 400 ms, at random.\n"
    "  --auto-interval <min>-<max>\n"
    "                   Interval range for --auto in ms, e.g. 100-300 (implies --auto).\n"
    "  --time-port <n>  Port answering Latency Cam: clock sync on UDP and uploads on TCP\n"
    "                   (default 48100).\n"
    "  --no-time-server Do not answer clock sync requests or accept uploads.\n"
    "  --results <dir>  Where uploaded recordings and their analysis go\n"
    "                   (default: Documents\\Latency Cam).\n"
    "  --analyzer <path> analyze_video.py to run on uploads (default: next to this app).\n"
    "  --python <path>  Python to run it with (default: python.exe or py.exe on PATH).\n"
    "  --help           Show this text.\n"
    "\n"
    "Any key press (except Esc) or mouse button press flips the trigger patch,\n"
    "as does --auto.\n"
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
        options.auto_flip = true;
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
        options.auto_flip = true;
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
