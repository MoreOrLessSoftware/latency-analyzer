/**
 * @file app/main.cpp
 * @brief Host side test app for measuring input to photon latency with a camera.
 *
 * Run it as an app on the streaming host (Sunshine, for example) and stream it to the client under test. It draws the test pattern described in
 * pattern.h and answers every key press (Esc quits) or mouse button press by presenting a frame with the trigger patch flipped,
 * as soon as the input arrives: it does not wait for the next background frame or, unless --vsync is passed, for
 * vertical blank. With --auto it also flips the trigger on its own at random intervals, on the next scheduled frame
 * rather than with an extra one, so the frame cadence stays steady. Each input and the frame
 * that answered it are logged to a CSV file with QueryPerformanceCounter timestamps, and every frame's present time to
 * a second CSV, so host side delays can be lined up with the streaming host's and client's logs.
 *
 * A UDP time server answers Latency Cam's clock sync requests (see clock_sync.h) with QueryPerformanceCounter values,
 * so what the camera films can be timed on the same clock as the logs.
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define WINVER 0x0A00

// local includes
#include "clock_sync.h"
#include "pattern.h"
#include "upload_server.h"

// platform includes (winsock2.h must come before windows.h)
#include <winsock2.h>
#include <mswsock.h>
#include <d2d1.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dwrite.h>
#include <dxgi1_6.h>
#include <shellapi.h>
#include <shlobj.h>
#include <commctrl.h>
#include <windows.h>
#include <tlhelp32.h>
#include <wrl/client.h>

// standard includes
#include <atomic>
#include <cstdio>
#include <deque>
#include <format>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <optional>
#include <random>
#include <thread>

using Microsoft::WRL::ComPtr;

namespace {
  /**
   * @brief Shaders that draw the whole test pattern from one fullscreen triangle.
   */
  constexpr char shader_source[] = R"(
cbuffer constants : register(b0) {
  uint frame;
  uint input_seq;
  float noise;
  uint pad0;
  uint clock_ms;
  uint since_input_ms;
  uint2 pad1;
  int4 readout;
  int4 digits;
  int4 trigger;
  int4 quiet;
  int4 fiducials[4];
  int4 frame_bits[8];
  int4 input_bits[4];
};

static const float4 black = float4(0, 0, 0, 1);
static const float4 white = float4(1, 1, 1, 1);

bool inside(int4 r, int2 p) {
  return p.x >= r.x && p.x < r.x + r.z && p.y >= r.y && p.y < r.y + r.w;
}

uint gray(uint v) {
  return v ^ (v >> 1);
}

uint hash(uint x) {
  x ^= x >> 16;
  x *= 0x7feb352dU;
  x ^= x >> 15;
  x *= 0x846ca68bU;
  x ^= x >> 16;
  return x;
}

float4 lit(uint bit) {
  return bit ? white : black;
}

// Seven segment bits: 1 top, 2 upper right, 4 lower right, 8 bottom, 16 lower left, 32 upper left, 64 middle.
static const uint segments[10] = {0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F};
static const uint pow10[7] = {1, 10, 100, 1000, 10000, 100000, 1000000};

// White digits on black: the clock in the first 7 slots, two empty slots, then the time since input.
float4 readout_color(int2 p) {
  int w = digits.z;
  int h = 2 * w;
  int advance = w + w / 4;
  int2 l = p - digits.xy;

  bool on = false;
  if (l.x >= 0 && l.y >= 0 && l.y < h) {
    uint slot = uint(l.x) / uint(advance);
    int u = l.x - int(slot) * advance;
    bool clock_slot = slot < 7;
    bool since_slot = slot >= 9 && slot < 13;
    if (u < w && (clock_slot || since_slot)) {
      uint digit = clock_slot ? (clock_ms / pow10[6 - slot]) % 10 : (since_input_ms / pow10[12 - slot]) % 10;
      uint seg = segments[digit];
      int t = max(w / 4, 1);
      int v = l.y;
      int middle = h / 2;
      on = on || ((seg & 1u) != 0 && v < t);
      on = on || ((seg & 8u) != 0 && v >= h - t);
      on = on || ((seg & 64u) != 0 && v >= middle - t / 2 && v < middle - t / 2 + t);
      on = on || ((seg & 32u) != 0 && u < t && v < middle);
      on = on || ((seg & 16u) != 0 && u < t && v >= middle);
      on = on || ((seg & 2u) != 0 && u >= w - t && v < middle);
      on = on || ((seg & 4u) != 0 && u >= w - t && v >= middle);
    }
  }
  return on ? white : black;
}

float4 vs_main(uint id : SV_VertexID) : SV_Position {
  float2 uv = float2((id << 1) & 2, id & 2);
  return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}

float4 ps_main(float4 pos : SV_Position) : SV_Target {
  int2 p = int2(pos.xy);
  float4 color;

  // The trigger and bit rows all sit inside the quiet zone; the fiducials are all outside it.
  if (inside(quiet, p)) {
    color = float4(0.5, 0.5, 0.5, 1);

    if (inside(trigger, p)) {
      color = lit(input_seq & 1);
    }

    // Gray code of the count modulo the row size, so the row wraps from all ones back to zero in one bit.
    uint frame_gray = gray(frame & 255u);
    [unroll] for (int f = 0; f < 8; ++f) {
      if (inside(frame_bits[f], p)) {
        color = lit((frame_gray >> (7 - f)) & 1);
      }
    }

    uint input_gray = gray(input_seq & 15u);
    [unroll] for (int i = 0; i < 4; ++i) {
      if (inside(input_bits[i], p)) {
        color = lit((input_gray >> (3 - i)) & 1);
      }
    }
  }
  else if (inside(readout, p)) {
    color = readout_color(p);
  }
  else {
    uint h = hash(uint(p.x) + hash(uint(p.y) + hash(frame)));
    float3 v = float3(h & 255, (h >> 8) & 255, (h >> 16) & 255) / 255.0;
    color = float4(0.5 + (v - 0.5) * noise, 1);

    // Square bullseye: white center, black ring, white outer ring.
    [unroll] for (int c = 0; c < 4; ++c) {
      int4 r = fiducials[c];
      if (inside(r, p)) {
        int2 d = abs(p - r.xy - (r.zw >> 1));
        uint ring = uint(max(d.x, d.y)) * 8u / uint(r.z);
        color = (ring == 0 || ring >= 3) ? white : black;
      }
    }
  }

  return color;
}
)";

  /**
   * @brief Constant buffer layout shared with the pixel shader.
   */
  struct constants_t {
    std::uint32_t frame;  ///< Frame number shown in the frame bits; also seeds the noise.
    std::uint32_t input_seq;  ///< Sequence number of the last input answered.
    float noise;  ///< Noise amplitude.
    std::uint32_t pad0;  ///< Padding to a 16 byte boundary.
    std::uint32_t clock_ms;  ///< Milliseconds since the app started, as of this frame.
    std::uint32_t since_input_ms;  ///< Milliseconds since the last input answered arrived, as of this frame.
    std::uint32_t pad1[2];  ///< Padding to a 16 byte boundary.
    std::int32_t readout[4];  ///< Readout band.
    std::int32_t digits[4];  ///< First digit position and digit width.
    std::int32_t trigger[4];  ///< Trigger patch.
    std::int32_t quiet[4];  ///< Quiet zone.
    std::int32_t fiducials[4][4];  ///< Corner fiducials.
    std::int32_t frame_bits[latency_test::frame_bit_count][4];  ///< Frame number patches.
    std::int32_t input_bits[latency_test::input_bit_count][4];  ///< Input sequence patches.
  };

  static_assert(sizeof(constants_t) % 16 == 0);

  /**
   * @brief An input waiting to be answered with a frame.
   */
  struct input_event_t {
    std::uint32_t seq;  ///< Sequence number, starting at 1.
    char source;  ///< 'k' for a key, 'm' for a mouse button, 'a' for an automatic flip.
    std::uint32_t detail;  ///< Virtual key code, or mouse button number.
    std::int64_t qpc;  ///< When the input reached the app.
  };

  /**
   * @brief Everything the window procedure and render loop share.
   */
  struct app_t {
    latency_test::options_t options;  ///< Command line options.
    HWND hwnd {};  ///< Test window.
    bool running = true;  ///< Cleared when the window closes.
    bool resize_pending = false;  ///< Set when the window size changed.
    bool toggle_vsync = false;  ///< Set when V was pressed.
    bool toggle_auto = false;  ///< Set when A was pressed.
    latency_test::upload_server_t *upload_server = nullptr;  ///< Latency Cam's upload server, when running.
    std::filesystem::path results_dir;  ///< Where uploads and their results are saved.
    bool exit_requested = false;  ///< Set when the app was closed during an upload or analysis, to wait for it.
    latency_test::fps_entry_t fps_entry;  ///< A frame rate being typed after F.
    std::optional<double> new_fps;  ///< A frame rate entered and not applied yet; 0 for the display's refresh rate.
    int width {};  ///< Current back buffer width.
    int height {};  ///< Current back buffer height.
    latency_test::layout_t layout {};  ///< Pattern layout for the current size.

    ComPtr<ID3D11Device> device;  ///< D3D11 device.
    ComPtr<ID3D11DeviceContext> context;  ///< Immediate context.
    ComPtr<IDXGISwapChain1> swap_chain;  ///< Flip model swap chain.
    ComPtr<ID3D11RenderTargetView> rtv;  ///< View of the back buffer.
    ComPtr<ID3D11VertexShader> vs;  ///< Fullscreen triangle shader.
    ComPtr<ID3D11PixelShader> ps;  ///< Pattern shader.
    ComPtr<ID3D11Buffer> constants;  ///< Pattern constants.
    bool tearing = false;  ///< Whether presenting with tearing is supported.
    UINT swap_flags = 0;  ///< Flags the swap chain was created with.

    ComPtr<ID2D1Factory> d2d;  ///< Direct2D factory, for the status text.
    ComPtr<ID2D1RenderTarget> text_target;  ///< Direct2D target on the back buffer.
    ComPtr<ID2D1SolidColorBrush> white;  ///< Text brush.
    ComPtr<ID2D1SolidColorBrush> black;  ///< Info band brush.
    ComPtr<IDWriteFactory> dwrite;  ///< DirectWrite factory.
    ComPtr<IDWriteTextFormat> text_format;  ///< Status text font, sized for the info band.
    latency_test::status_t status;  ///< What the status text shows.
    std::wstring status_text;  ///< The status text as drawn.
    std::int64_t rate_window_qpc = 0;  ///< Start of the second the presented frame rate is counted over.
    std::uint32_t rate_frames = 0;  ///< Frames presented so far in that second.
    std::int64_t rate_wait_qpc = 0;  ///< Time spent inside Present so far in that second.
    std::optional<double> rtss_fps;  ///< RivaTuner Statistics Server's frame rate limit for this app, if it runs one.

    std::deque<input_event_t> pending;  ///< Inputs not answered yet.
    std::uint32_t next_seq = 1;  ///< Sequence number for the next input.
    std::uint32_t shown_seq = 0;  ///< Sequence number currently shown.
    std::int64_t shown_input_qpc = 0;  ///< When the input currently shown arrived; 0 before any input.
    std::uint32_t frame = 0;  ///< Frames presented so far.
    bool keys_down[256] {};  ///< Keys currently held, to ignore auto repeat.

    std::FILE *log {};  ///< CSV log of inputs.
    std::FILE *frames_log {};  ///< CSV log of every frame.
    std::string log_path;  ///< Path of the input log.
    std::int64_t frames_flushed_qpc = 0;  ///< When the frames log was last flushed.
    std::int64_t qpc_start {};  ///< QueryPerformanceCounter value logged times are relative to.
    std::int64_t qpc_frequency {};  ///< QueryPerformanceCounter ticks per second.

    std::mt19937 rng;  ///< Picks the intervals between automatic flips.
    std::int64_t next_auto_qpc = 0;  ///< When the next automatic flip is due.
  };

  /**
   * @brief The thread answering clock sync requests.
   */
  struct time_server_t {
    SOCKET socket = INVALID_SOCKET;  ///< Bound UDP socket.
    std::thread thread;  ///< Thread waiting for requests.
    std::atomic<bool> running {false};  ///< Cleared to stop the thread.
    std::atomic<std::uint32_t> answered {0};  ///< Requests answered so far.
    bool winsock = false;  ///< Whether WSAStartup succeeded and needs a WSACleanup.
  };

  /**
   * @brief Read QueryPerformanceCounter.
   *
   * @return The counter value.
   */
  std::int64_t qpc_now() {
    LARGE_INTEGER value;
    QueryPerformanceCounter(&value);
    return value.QuadPart;
  }

  /**
   * @brief Convert a counter value to microseconds since the app started.
   *
   * @param app App state.
   * @param qpc Counter value.
   * @return Microseconds since start.
   */
  double qpc_to_us(const app_t &app, std::int64_t qpc) {
    return static_cast<double>(qpc - app.qpc_start) * 1e6 / static_cast<double>(app.qpc_frequency);
  }

  /**
   * @brief Answer clock sync requests until the server is stopped.
   *
   * The arrival time is read as soon as the request is received and the reply time just before it is sent, so the
   * time the host holds a request is left out of the round trip.
   *
   * @param server Time server.
   * @param frequency QueryPerformanceCounter ticks per second.
   */
  void serve_time(time_server_t &server, std::int64_t frequency) {
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
    std::uint8_t buffer[64];
    while (server.running) {
      sockaddr_in from {};
      int from_size = sizeof(from);
      const int received = recvfrom(server.socket, reinterpret_cast<char *>(buffer), sizeof(buffer), 0, reinterpret_cast<sockaddr *>(&from), &from_size);
      const std::int64_t t2 = qpc_now();
      if (received == SOCKET_ERROR) {
        // Closing the socket ends the wait; anything else (such as a reset from an earlier reply) is retried
        if (!server.running) {
          break;
        }
        continue;
      }
      const auto request = latency_test::parse_clock_request(buffer, static_cast<std::size_t>(received));
      if (!request) {
        continue;
      }
      const auto reply = latency_test::make_clock_reply(*request, t2, qpc_now(), frequency);
      sendto(server.socket, reinterpret_cast<const char *>(reply.data()), static_cast<int>(reply.size()), 0, reinterpret_cast<const sockaddr *>(&from), from_size);
      ++server.answered;
    }
  }

  /**
   * @brief Start answering clock sync requests on a UDP port.
   *
   * @param server Time server to start.
   * @param port UDP port.
   * @param frequency QueryPerformanceCounter ticks per second.
   * @return Empty on success, otherwise what went wrong.
   */
  std::string start_time_server(time_server_t &server, int port, std::int64_t frequency) {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
      return "WSAStartup failed";
    }
    server.winsock = true;
    server.socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (server.socket == INVALID_SOCKET) {
      return std::format("socket failed ({})", WSAGetLastError());
    }
    // Without this, a reply to a phone that has gone away fails the next receive with WSAECONNRESET
    BOOL report_reset = FALSE;
    DWORD returned = 0;
    WSAIoctl(server.socket, SIO_UDP_CONNRESET, &report_reset, sizeof(report_reset), nullptr, 0, &returned, nullptr, nullptr);

    sockaddr_in address {};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<u_short>(port));
    address.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(server.socket, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) == SOCKET_ERROR) {
      const int code = WSAGetLastError();
      closesocket(server.socket);
      server.socket = INVALID_SOCKET;
      return std::format("port {} is in use or blocked ({})", port, code);
    }
    server.running = true;
    server.thread = std::thread(serve_time, std::ref(server), frequency);
    return {};
  }

  /**
   * @brief Stop the time server if it is running.
   *
   * @param server Time server.
   */
  void stop_time_server(time_server_t &server) {
    server.running = false;
    if (server.socket != INVALID_SOCKET) {
      closesocket(server.socket);
      server.socket = INVALID_SOCKET;
    }
    if (server.thread.joinable()) {
      server.thread.join();
    }
    if (server.winsock) {
      WSACleanup();
      server.winsock = false;
    }
  }

  /**
   * @brief Convert UTF-16 to UTF-8.
   *
   * @param text Text to convert.
   * @return The UTF-8 text.
   */
  std::string to_utf8(std::wstring_view text) {
    const int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    std::string result(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size, nullptr, nullptr);
    return result;
  }

  /**
   * @brief Convert UTF-8 to UTF-16.
   *
   * @param text Text to convert.
   * @return The UTF-16 text.
   */
  std::wstring to_utf16(std::string_view text) {
    const int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring result(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size);
    return result;
  }

  /**
   * @brief Show an error and return a failing exit code.
   *
   * @param what What failed.
   * @param hr Result code.
   * @return Exit code 1.
   */
  int fail(std::string_view what, HRESULT hr) {
    const auto message = std::format("{} failed [0x{:08x}]", what, static_cast<unsigned>(hr));
    MessageBoxW(nullptr, to_utf16(message).c_str(), L"latency-test", MB_ICONERROR);
    return 1;
  }

  /**
   * @brief Copy a rectangle into a constant buffer slot.
   *
   * @param out Slot to fill.
   * @param rect Rectangle to copy.
   */
  void put_rect(std::int32_t (&out)[4], const latency_test::rect_t &rect) {
    out[0] = rect.x;
    out[1] = rect.y;
    out[2] = rect.w;
    out[3] = rect.h;
  }

  /**
   * @brief Queue an input to be answered by the next frame.
   *
   * @param app App state.
   * @param source 'k' for a key, 'm' for a mouse button.
   * @param detail Virtual key code, or mouse button number.
   * @param qpc When the input arrived.
   */
  void queue_input(app_t &app, char source, std::uint32_t detail, std::int64_t qpc) {
    app.pending.push_back({app.next_seq++, source, detail, qpc});
  }

  /**
   * @brief Turn a raw input message into queued inputs.
   *
   * Only presses count: key releases, key auto repeat and mouse movement are ignored.
   *
   * @param app App state.
   * @param handle Raw input handle from WM_INPUT.
   */
  void handle_raw_input(app_t &app, HRAWINPUT handle) {
    const std::int64_t qpc = qpc_now();

    RAWINPUT raw {};
    UINT size = sizeof(raw);
    if (GetRawInputData(handle, RID_INPUT, &raw, &size, sizeof(RAWINPUTHEADER)) == static_cast<UINT>(-1)) {
      return;
    }

    if (raw.header.dwType == RIM_TYPEKEYBOARD) {
      const USHORT vkey = raw.data.keyboard.VKey;
      if (vkey >= 256) {
        return;
      }
      if (raw.data.keyboard.Flags & RI_KEY_BREAK) {
        app.keys_down[vkey] = false;
        return;
      }
      if (app.keys_down[vkey]) {
        return;  // Auto repeat
      }
      app.keys_down[vkey] = true;

      if (app.fps_entry.active) {
        // While a frame rate is typed, keys edit it instead of flipping the trigger, and Esc cancels
        double fps = 0;
        if (app.fps_entry.key(vkey, fps) == latency_test::fps_entry_t::result_e::apply) {
          app.new_fps = fps;
        }
        app.status_text.clear();
      }
      else if (vkey == VK_ESCAPE) {
        // Esc quits instead of counting as an input
        PostMessageW(app.hwnd, WM_CLOSE, 0, 0);
      }
      else if (vkey == 'V') {
        // V toggles v-sync, A automatic flips and F starts typing a frame rate, instead of counting as inputs
        app.toggle_vsync = true;
      }
      else if (vkey == 'A') {
        app.toggle_auto = true;
      }
      else if (vkey == 'F') {
        app.fps_entry.start();
        app.status_text.clear();
      }
      else {
        queue_input(app, 'k', vkey, qpc);
      }
    }
    else if (raw.header.dwType == RIM_TYPEMOUSE) {
      constexpr USHORT downs[] = {
        RI_MOUSE_BUTTON_1_DOWN,
        RI_MOUSE_BUTTON_2_DOWN,
        RI_MOUSE_BUTTON_3_DOWN,
        RI_MOUSE_BUTTON_4_DOWN,
        RI_MOUSE_BUTTON_5_DOWN
      };
      for (std::uint32_t button = 0; button < std::size(downs); ++button) {
        if (raw.data.mouse.usButtonFlags & downs[button]) {
          queue_input(app, 'm', button + 1, qpc);
        }
      }
    }
  }

  /**
   * @brief Window procedure of the test window.
   *
   * @param hwnd Window.
   * @param msg Message.
   * @param wparam Message parameter.
   * @param lparam Message parameter.
   * @return Message result.
   */
  LRESULT CALLBACK window_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    auto *app = reinterpret_cast<app_t *>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
      case WM_INPUT:
        if (app) {
          handle_raw_input(*app, reinterpret_cast<HRAWINPUT>(lparam));
        }
        break;
      case WM_SIZE:
        if (app && LOWORD(lparam) > 0 && HIWORD(lparam) > 0) {
          app->resize_pending = true;
        }
        return 0;
      case WM_SETCURSOR:
        // Hide the cursor so the streaming host does not draw it over the pattern.
        if (LOWORD(lparam) == HTCLIENT) {
          SetCursor(nullptr);
          return TRUE;
        }
        break;
      case WM_CLOSE:
        if (app && app->exit_requested) {
          return 0;  // Already closing; Esc pressed again changes nothing
        }
        if (app && app->upload_server && app->upload_server->activity().busy) {
          // Close the test pattern now; the main loop shows the progress until the upload or analysis is done
          app->exit_requested = true;
          ShowWindow(hwnd, SW_HIDE);
          return 0;
        }
        break;
      case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wparam, lparam);
  }

  /**
   * @brief Create the render target view for the current back buffer and update the layout.
   *
   * @param app App state.
   * @return Result code.
   */
  HRESULT create_target(app_t &app) {
    RECT client;
    GetClientRect(app.hwnd, &client);
    app.width = client.right - client.left;
    app.height = client.bottom - client.top;
    app.layout = latency_test::compute_layout(app.width, app.height);

    ComPtr<ID3D11Texture2D> back_buffer;
    HRESULT hr = app.swap_chain->GetBuffer(0, IID_PPV_ARGS(&back_buffer));
    if (FAILED(hr)) {
      return hr;
    }
    hr = app.device->CreateRenderTargetView(back_buffer.Get(), nullptr, &app.rtv);
    if (FAILED(hr)) {
      return hr;
    }

    const D3D11_VIEWPORT viewport {0, 0, static_cast<float>(app.width), static_cast<float>(app.height), 0, 1};
    app.context->RSSetViewports(1, &viewport);

    // The status text is drawn with Direct2D on the same back buffer. Without it the pattern still works.
    app.text_target.Reset();
    app.white.Reset();
    app.black.Reset();
    app.text_format.Reset();
    ComPtr<IDXGISurface> surface;
    if (app.d2d && app.dwrite && SUCCEEDED(back_buffer.As(&surface))) {
      const auto properties = D2D1::RenderTargetProperties(D2D1_RENDER_TARGET_TYPE_DEFAULT, D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED), 96, 96);
      if (SUCCEEDED(app.d2d->CreateDxgiSurfaceRenderTarget(surface.Get(), &properties, &app.text_target))) {
        app.text_target->CreateSolidColorBrush(D2D1::ColorF(D2D1::ColorF::White), &app.white);
        app.text_target->CreateSolidColorBrush(D2D1::ColorF(D2D1::ColorF::Black), &app.black);
        // Two lines in the band, each up to about 90 characters across it
        const auto &info = app.layout.info;
        const float size = std::max(8.0f, std::min(info.h * 0.3f, info.w / 48.0f));
        app.dwrite->CreateTextFormat(L"Segoe UI", nullptr, DWRITE_FONT_WEIGHT_SEMI_BOLD, DWRITE_FONT_STYLE_NORMAL, DWRITE_FONT_STRETCH_NORMAL, size, L"en-us", &app.text_format);
        if (app.text_format) {
          app.text_format->SetTextAlignment(DWRITE_TEXT_ALIGNMENT_CENTER);
          app.text_format->SetParagraphAlignment(DWRITE_PARAGRAPH_ALIGNMENT_CENTER);
          app.text_format->SetWordWrapping(DWRITE_WORD_WRAPPING_NO_WRAP);
        }
      }
    }
    return S_OK;
  }

  /**
   * @brief Resize the swap chain to the window.
   *
   * @param app App state.
   * @return Result code.
   */
  HRESULT resize(app_t &app) {
    app.resize_pending = false;
    app.context->OMSetRenderTargets(0, nullptr, nullptr);
    app.rtv.Reset();
    app.text_target.Reset();
    app.white.Reset();
    app.black.Reset();
    const HRESULT hr = app.swap_chain->ResizeBuffers(0, 0, 0, DXGI_FORMAT_UNKNOWN, app.swap_flags);
    return FAILED(hr) ? hr : create_target(app);
  }

  /**
   * @brief Create the device, swap chain and shaders.
   *
   * @param app App state.
   * @return Exit code, 0 on success.
   */
  int init_d3d(app_t &app) {
    UINT device_flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, device_flags, nullptr, 0, D3D11_SDK_VERSION, &app.device, nullptr, &app.context);
    if (FAILED(hr)) {
      return fail("D3D11CreateDevice", hr);
    }

    ComPtr<IDXGIDevice1> dxgi_device;
    app.device.As(&dxgi_device);
    // One frame in flight at most, so an input is never stuck behind queued frames.
    dxgi_device->SetMaximumFrameLatency(1);

    ComPtr<IDXGIAdapter> adapter;
    dxgi_device->GetAdapter(&adapter);
    ComPtr<IDXGIFactory2> factory;
    hr = adapter->GetParent(IID_PPV_ARGS(&factory));
    if (FAILED(hr)) {
      return fail("IDXGIAdapter::GetParent", hr);
    }

    ComPtr<IDXGIFactory5> factory5;
    if (SUCCEEDED(factory.As(&factory5))) {
      BOOL allow = FALSE;
      if (SUCCEEDED(factory5->CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING, &allow, sizeof(allow)))) {
        app.tearing = allow;
      }
    }
    app.swap_flags = app.tearing ? DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING : 0;

    DXGI_SWAP_CHAIN_DESC1 desc {};
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 2;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    desc.Flags = app.swap_flags;
    hr = factory->CreateSwapChainForHwnd(app.device.Get(), app.hwnd, &desc, nullptr, nullptr, &app.swap_chain);
    if (FAILED(hr)) {
      return fail("CreateSwapChainForHwnd", hr);
    }
    factory->MakeWindowAssociation(app.hwnd, DXGI_MWA_NO_ALT_ENTER);

    D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, app.d2d.GetAddressOf());
    DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED, __uuidof(IDWriteFactory), reinterpret_cast<IUnknown **>(app.dwrite.GetAddressOf()));

    hr = create_target(app);
    if (FAILED(hr)) {
      return fail("Creating the render target", hr);
    }

    ComPtr<ID3DBlob> vs_blob;
    ComPtr<ID3DBlob> ps_blob;
    ComPtr<ID3DBlob> errors;
    hr = D3DCompile(shader_source, sizeof(shader_source) - 1, "pattern", nullptr, nullptr, "vs_main", "vs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &vs_blob, &errors);
    if (SUCCEEDED(hr)) {
      hr = D3DCompile(shader_source, sizeof(shader_source) - 1, "pattern", nullptr, nullptr, "ps_main", "ps_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &ps_blob, &errors);
    }
    if (FAILED(hr)) {
      const std::string details = errors ? std::string(static_cast<const char *>(errors->GetBufferPointer()), errors->GetBufferSize()) : "";
      return fail("Compiling shaders: " + details, hr);
    }
    app.device->CreateVertexShader(vs_blob->GetBufferPointer(), vs_blob->GetBufferSize(), nullptr, &app.vs);
    app.device->CreatePixelShader(ps_blob->GetBufferPointer(), ps_blob->GetBufferSize(), nullptr, &app.ps);

    D3D11_BUFFER_DESC cb_desc {};
    cb_desc.ByteWidth = sizeof(constants_t);
    cb_desc.Usage = D3D11_USAGE_DYNAMIC;
    cb_desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb_desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = app.device->CreateBuffer(&cb_desc, nullptr, &app.constants);
    if (FAILED(hr)) {
      return fail("Creating the constant buffer", hr);
    }

    return 0;
  }

  /**
   * @brief Count presented frames and refresh the status text once a second.
   *
   * @param app App state.
   * @param present_qpc When Present was called for the frame just presented.
   * @param presented_qpc When the frame just presented returned from Present.
   */
  void update_status(app_t &app, std::int64_t present_qpc, std::int64_t presented_qpc) {
    ++app.rate_frames;
    app.rate_wait_qpc += presented_qpc - present_qpc;
    if (app.rate_window_qpc == 0) {
      app.rate_window_qpc = presented_qpc;
      app.rate_frames = 0;
      app.rate_wait_qpc = 0;
    }
    const std::int64_t elapsed = presented_qpc - app.rate_window_qpc;
    if (elapsed >= app.qpc_frequency || app.status_text.empty()) {
      if (elapsed >= app.qpc_frequency) {
        app.status.measured_fps = app.rate_frames * static_cast<double>(app.qpc_frequency) / static_cast<double>(elapsed);
        app.status.present_wait_ms = app.rate_frames ? app.rate_wait_qpc * 1000.0 / static_cast<double>(app.qpc_frequency) / app.rate_frames : 0;
        app.rate_window_qpc = presented_qpc;
        app.rate_frames = 0;
        app.rate_wait_qpc = 0;
      }
      app.status.entering_fps = app.fps_entry.active;
      app.status.fps_entry = app.fps_entry.text;
      app.status.fps_entry_invalid = app.fps_entry.invalid;
      app.status_text = to_utf16(latency_test::status_text(app.status));
    }
  }

  /**
   * @brief Draw the status text in the info band.
   *
   * @param app App state.
   */
  void draw_status(app_t &app) {
    if (!app.text_target || !app.text_format || app.status_text.empty()) {
      return;
    }
    const auto &info = app.layout.info;
    const D2D1_RECT_F band {static_cast<float>(info.x), static_cast<float>(info.y), static_cast<float>(info.x + info.w), static_cast<float>(info.y + info.h)};
    app.text_target->BeginDraw();
    app.text_target->FillRectangle(band, app.black.Get());
    app.text_target->DrawText(app.status_text.data(), static_cast<UINT32>(app.status_text.size()), app.text_format.Get(), band, app.white.Get());
    if (app.text_target->EndDraw() == D2DERR_RECREATE_TARGET) {
      create_target(app);
    }
  }

  /**
   * @brief Draw and present one frame, answering the oldest pending input if there is one.
   *
   * @param app App state.
   */
  void render(app_t &app) {
    const std::int64_t render_qpc = qpc_now();
    const bool answering = !app.pending.empty();
    if (answering) {
      app.shown_seq = app.pending.front().seq;
      app.shown_input_qpc = app.pending.front().qpc;
    }

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (SUCCEEDED(app.context->Map(app.constants.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
      auto &c = *static_cast<constants_t *>(mapped.pData);
      c = {};
      c.frame = app.frame;
      c.input_seq = app.shown_seq;
      c.noise = static_cast<float>(app.options.noise);
      c.clock_ms = static_cast<std::uint32_t>(qpc_to_us(app, render_qpc) / 1000) % 10'000'000;
      c.since_input_ms = app.shown_input_qpc == 0 ? 9999 : std::min<std::uint32_t>(static_cast<std::uint32_t>((render_qpc - app.shown_input_qpc) * 1000 / app.qpc_frequency), 9999);
      put_rect(c.readout, app.layout.readout);
      c.digits[0] = app.layout.digit_x;
      c.digits[1] = app.layout.digit_y;
      c.digits[2] = app.layout.digit_w;
      put_rect(c.trigger, app.layout.trigger);
      put_rect(c.quiet, app.layout.quiet);
      for (int i = 0; i < 4; ++i) {
        put_rect(c.fiducials[i], app.layout.fiducials[i]);
      }
      for (int i = 0; i < latency_test::frame_bit_count; ++i) {
        put_rect(c.frame_bits[i], app.layout.frame_bits[i]);
      }
      for (int i = 0; i < latency_test::input_bit_count; ++i) {
        put_rect(c.input_bits[i], app.layout.input_bits[i]);
      }
      app.context->Unmap(app.constants.Get(), 0);
    }

    // Set every frame, since Direct2D shares the device and may change the pipeline state
    app.context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    app.context->IASetInputLayout(nullptr);
    app.context->VSSetShader(app.vs.Get(), nullptr, 0);
    app.context->PSSetShader(app.ps.Get(), nullptr, 0);
    app.context->PSSetConstantBuffers(0, 1, app.constants.GetAddressOf());
    app.context->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    app.context->OMSetDepthStencilState(nullptr, 0);
    app.context->RSSetState(nullptr);
    const D3D11_VIEWPORT viewport {0, 0, static_cast<float>(app.width), static_cast<float>(app.height), 0, 1};
    app.context->RSSetViewports(1, &viewport);
    app.context->OMSetRenderTargets(1, app.rtv.GetAddressOf(), nullptr);
    app.context->Draw(3, 0);
    draw_status(app);

    const UINT sync_interval = app.options.vsync ? 1 : 0;
    const UINT present_flags = !app.options.vsync && app.tearing ? DXGI_PRESENT_ALLOW_TEARING : 0;
    const std::int64_t present_qpc = qpc_now();
    app.swap_chain->Present(sync_interval, present_flags);
    const std::int64_t presented_qpc = qpc_now();
    update_status(app, present_qpc, presented_qpc);

    if (app.frames_log) {
      std::fputs(
        std::format(
          "{},{:.1f},{:.1f},{:.1f},{},{}\n",
          app.frame,
          qpc_to_us(app, render_qpc),
          qpc_to_us(app, present_qpc),
          qpc_to_us(app, presented_qpc),
          app.shown_seq,
          app.options.vsync ? 1 : 0
        )
          .c_str(),
        app.frames_log
      );
      // Flushed about once a second rather than every frame
      if (presented_qpc - app.frames_flushed_qpc > app.qpc_frequency) {
        std::fflush(app.frames_log);
        app.frames_flushed_qpc = presented_qpc;
      }
    }

    if (answering) {
      const input_event_t event = app.pending.front();
      app.pending.pop_front();
      if (app.log) {
        std::fputs(
          std::format(
            "{},{},{},{:.1f},{:.1f},{:.1f},{:.1f},{},{}\n",
            event.seq,
            event.source,
            event.detail,
            qpc_to_us(app, event.qpc),
            qpc_to_us(app, render_qpc),
            qpc_to_us(app, present_qpc),
            qpc_to_us(app, presented_qpc),
            app.frame,
            app.pending.size()
          )
            .c_str(),
          app.log
        );
        std::fflush(app.log);
      }
    }
    ++app.frame;
  }

  /**
   * @brief Read a whole file.
   *
   * @param path File to read.
   * @return Its contents, or nothing when it can't be read.
   */
  std::optional<std::string> read_file(const std::wstring &path) {
    std::ifstream file(std::filesystem::path(path), std::ios::binary);
    if (!file) {
      return std::nullopt;
    }
    return std::string(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
  }

  /**
   * @brief Whether a process with this executable name is running.
   *
   * @param name Executable file name, such as RTSS.exe.
   * @return True when one is.
   */
  bool process_running(const wchar_t *name) {
    const HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) {
      return false;
    }
    PROCESSENTRY32W entry {};
    entry.dwSize = sizeof(entry);
    bool found = false;
    for (BOOL more = Process32FirstW(snapshot, &entry); more && !found; more = Process32NextW(snapshot, &entry)) {
      found = _wcsicmp(entry.szExeFile, name) == 0;
    }
    CloseHandle(snapshot);
    return found;
  }

  /**
   * @brief The frame rate limit RivaTuner Statistics Server applies to this app, read from its profiles.
   *
   * RTSS keeps its settings in INI files in its Profiles folder: one per app (named after the executable) and a global
   * one for the rest. Only counts while RTSS is running.
   *
   * @return The limit in fps, or nothing when RTSS isn't running or sets no limit.
   */
  std::optional<double> read_rtss_limit() {
    if (!process_running(L"RTSS.exe")) {
      return std::nullopt;
    }
    wchar_t dir[MAX_PATH] {};
    DWORD size = sizeof(dir);
    if (RegGetValueW(HKEY_LOCAL_MACHINE, L"SOFTWARE\\WOW6432Node\\Unwinder\\RTSS", L"InstallDir", RRF_RT_REG_SZ, nullptr, dir, &size) != ERROR_SUCCESS) {
      return std::nullopt;
    }
    const std::wstring profiles = std::wstring(dir) + L"\\Profiles\\";
    wchar_t exe[MAX_PATH] {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    const wchar_t *name = wcsrchr(exe, L'\\');
    const auto app_profile = read_file(profiles + (name ? name + 1 : exe) + L".cfg");
    const auto global_profile = read_file(profiles + L"Global");
    return latency_test::rtss_limit(
      app_profile ? std::optional<std::string_view>(*app_profile) : std::nullopt,
      global_profile ? std::optional<std::string_view>(*global_profile) : std::nullopt
    );
  }

  /**
   * @brief Get the refresh rate of the monitor a window is on.
   *
   * @param hwnd Window.
   * @return Refresh rate in Hz, or 0 when it is unknown.
   */
  int refresh_rate(HWND hwnd) {
    MONITORINFOEXW info {};
    info.cbSize = sizeof(info);
    GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTOPRIMARY), &info);
    DEVMODEW mode {};
    mode.dmSize = sizeof(mode);
    if (!EnumDisplaySettingsW(info.szDevice, ENUM_CURRENT_SETTINGS, &mode) || mode.dmDisplayFrequency <= 1) {
      return 0;
    }
    return static_cast<int>(mode.dmDisplayFrequency);
  }

  /**
   * @brief Open the CSV log and write its header.
   *
   * Both logs start with the same comment lines, so either one can be read on its own.
   *
   * @param app App state.
   * @param refresh Display refresh rate.
   * @param time_server How the time server started: "port N", "off" or what went wrong.
   */
  void open_log(app_t &app, int refresh, std::string_view time_server) {
    std::string path = app.options.log_path;
    if (path.empty()) {
      wchar_t temp[MAX_PATH + 1] {};
      GetTempPathW(MAX_PATH + 1, temp);
      path = to_utf8(temp) + "latency_test.csv";
    }
    app.log_path = path;
    app.log = _wfopen(to_utf16(path).c_str(), L"w");
    app.frames_log = _wfopen(to_utf16(latency_test::frames_log_path(path)).c_str(), L"w");

    FILETIME file_time;
    GetSystemTimePreciseAsFileTime(&file_time);
    SYSTEMTIME utc;
    FileTimeToSystemTime(&file_time, &utc);
    const auto start_ticks = (static_cast<std::uint64_t>(file_time.dwHighDateTime) << 32) | file_time.dwLowDateTime;

    const std::string header = std::format(
      "# latency-test\n"
      "# start_utc={:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z start_filetime={} start_qpc={} qpc_frequency={}\n"
      "# width={} height={} refresh_hz={} fps={} fps_source={} rtss_limit={} vsync={} tearing={} noise={}\n"
      "# auto={} auto_min_ms={} auto_max_ms={} time_server={}\n"
      "# Times are microseconds of QueryPerformanceCounter since start_qpc.\n",
      utc.wYear,
      utc.wMonth,
      utc.wDay,
      utc.wHour,
      utc.wMinute,
      utc.wSecond,
      utc.wMilliseconds,
      start_ticks,
      app.qpc_start,
      app.qpc_frequency,
      app.width,
      app.height,
      refresh,
      app.options.fps,
      latency_test::fps_source_name(app.status.fps_source),
      app.rtss_fps.value_or(0),
      app.options.vsync,
      app.tearing,
      app.options.noise,
      app.options.auto_flip,
      app.options.auto_min_ms,
      app.options.auto_max_ms,
      time_server
    );
    if (app.log) {
      std::fputs((header + "seq,source,detail,input_us,render_us,present_call_us,present_return_us,frame,still_queued\n").c_str(), app.log);
      std::fflush(app.log);
    }
    if (app.frames_log) {
      std::fputs((header + "frame,render_us,present_call_us,present_return_us,input_seq,vsync\n").c_str(), app.frames_log);
      std::fflush(app.frames_log);
    }
  }

  /**
   * @brief Turn v-sync on or off, and note the change in the frames log and on screen.
   *
   * @param app App state.
   * @param on Whether presents should wait for vertical blank.
   */
  void set_vsync(app_t &app, bool on) {
    app.options.vsync = on;
    app.status.vsync = on;
    app.status.tearing = !on && app.tearing;
    // Count the presented rate afresh, and redraw the text on the next frame
    app.rate_window_qpc = 0;
    app.status.measured_fps = 0;
    app.status_text.clear();
    if (app.frames_log) {
      std::fputs(std::format("# vsync={} from frame {}\n", on ? 1 : 0, app.frame).c_str(), app.frames_log);
    }
  }

  /**
   * @brief Default folder for Latency Cam uploads: a results folder next to the executable, so a copied folder is
   * self-contained, or Latency Cam in the user's Documents when that can't be written (e.g. under Program Files).
   *
   * @return The folder.
   */
  std::filesystem::path default_results_dir() {
    wchar_t module[MAX_PATH] {};
    GetModuleFileNameW(nullptr, module, MAX_PATH);
    const auto portable = std::filesystem::path(module).parent_path() / L"results";
    std::error_code error;
    std::filesystem::create_directories(portable, error);
    if (!error) {
      // Creating the folder can succeed under virtualization while files can't be written, so try one
      const auto probe = portable / L".write-test";
      const HANDLE file = CreateFileW(probe.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
      if (file != INVALID_HANDLE_VALUE) {
        CloseHandle(file);
        return portable;
      }
    }

    PWSTR documents = nullptr;
    std::filesystem::path dir;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &documents))) {
      dir = std::filesystem::path(documents) / L"Latency Cam";
    }
    else {
      wchar_t temp[MAX_PATH + 1] {};
      GetTempPathW(MAX_PATH + 1, temp);
      dir = std::filesystem::path(temp) / L"Latency Cam";
    }
    CoTaskMemFree(documents);
    return dir;
  }

  /**
   * @brief Pick when the next automatic flip is due.
   *
   * @param app App state.
   * @param now Current QueryPerformanceCounter value.
   */
  void schedule_auto_flip(app_t &app, std::int64_t now) {
    std::uniform_real_distribution<double> interval_ms(app.options.auto_min_ms, app.options.auto_max_ms);
    app.next_auto_qpc = now + static_cast<std::int64_t>(interval_ms(app.rng) * static_cast<double>(app.qpc_frequency) / 1000);
  }

  /**
   * @brief Turn automatic flips on or off, and note the change in both logs and on screen.
   *
   * @param app App state.
   * @param on Whether the trigger should flip on its own.
   * @param now Current QueryPerformanceCounter value; the first flip comes one interval after it.
   */
  void set_auto_flip(app_t &app, bool on, std::int64_t now) {
    app.options.auto_flip = on;
    app.status.auto_flip = on;
    app.status_text.clear();
    if (on) {
      schedule_auto_flip(app, now);
    }
    const std::string note = std::format("# auto={} from frame {}\n", on ? 1 : 0, app.frame);
    for (std::FILE *log : {app.log, app.frames_log}) {
      if (log) {
        std::fputs(note.c_str(), log);
      }
    }
  }

  /**
   * @brief Change the background frame rate, and note the change in both logs and on screen.
   *
   * @param app App state.
   * @param fps New frame rate, or 0 to go back to the default: RTSS's limit, else the display's refresh rate.
   */
  void set_fps(app_t &app, double fps) {
    app.status.fps_source = fps > 0 ? latency_test::fps_source_e::option : app.status.default_source;
    app.options.fps = fps > 0 ? fps : app.rtss_fps ? *app.rtss_fps : (app.status.refresh_hz > 0 ? app.status.refresh_hz : 60);
    app.status.fps = app.options.fps;
    // Count the presented rate afresh, and redraw the text on the next frame
    app.rate_window_qpc = 0;
    app.status.measured_fps = 0;
    app.status_text.clear();
    const std::string note = std::format("# fps={} fps_source={} from frame {}\n", app.options.fps, latency_test::fps_source_name(app.status.fps_source), app.frame);
    for (std::FILE *log : {app.log, app.frames_log}) {
      if (log) {
        std::fputs(note.c_str(), log);
      }
    }
  }

  /**
   * @brief State of the window shown while the app waits to close.
   */
  struct wait_window_t {
    app_t *app = nullptr;  ///< App whose upload server is busy.
    HWND text {};  ///< What is in progress.
    HWND progress {};  ///< Progress bar.
    bool marquee = false;  ///< Whether the bar is in its indeterminate mode.
    bool done = false;  ///< Set when the window should close.
  };

  constexpr UINT_PTR wait_timer_id = 1;  ///< Timer refreshing the wait window.
  constexpr int close_anyway_id = 100;  ///< Command of the Close anyway button.

  /**
   * @brief Refresh the wait window from the upload server, closing it when nothing is in progress.
   *
   * @param hwnd Wait window.
   * @param state Its state.
   */
  void refresh_wait_window(HWND hwnd, wait_window_t &state) {
    const auto activity = state.app->upload_server->activity();
    if (!activity.busy) {
      state.done = true;
      DestroyWindow(hwnd);
      return;
    }
    SetWindowTextW(state.text, to_utf16(activity.description + "\n\nThis window closes when it's done.").c_str());
    const bool marquee = activity.percent < 0;
    if (marquee != state.marquee) {
      const LONG_PTR style = GetWindowLongPtrW(state.progress, GWL_STYLE);
      SetWindowLongPtrW(state.progress, GWL_STYLE, marquee ? style | PBS_MARQUEE : style & ~static_cast<LONG_PTR>(PBS_MARQUEE));
      SendMessageW(state.progress, PBM_SETMARQUEE, marquee, 30);
      state.marquee = marquee;
    }
    if (!marquee) {
      SendMessageW(state.progress, PBM_SETPOS, activity.percent, 0);
    }
  }

  /**
   * @brief Window procedure of the wait window.
   *
   * @param hwnd Window.
   * @param msg Message.
   * @param wparam Message parameter.
   * @param lparam Message parameter.
   * @return Message result.
   */
  LRESULT CALLBACK wait_window_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    auto *state = reinterpret_cast<wait_window_t *>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
      case WM_TIMER:
        if (state && !state->done) {
          refresh_wait_window(hwnd, *state);
        }
        return 0;
      case WM_COMMAND:
        if (LOWORD(wparam) == close_anyway_id && state) {
          state->done = true;
          DestroyWindow(hwnd);
        }
        return 0;
      case WM_CLOSE:
        // The window's X closes anyway too
        if (state) {
          state->done = true;
        }
        DestroyWindow(hwnd);
        return 0;
      case WM_DESTROY:
        KillTimer(hwnd, wait_timer_id);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wparam, lparam);
  }

  /**
   * @brief After the test pattern closed during an upload or analysis, show its progress until it finishes or the user
   * chooses Close anyway.
   *
   * @param app App state.
   */
  void wait_before_closing(app_t &app) {
    const INITCOMMONCONTROLSEX controls {sizeof(INITCOMMONCONTROLSEX), ICC_PROGRESS_CLASS};
    InitCommonControlsEx(&controls);

    const HINSTANCE instance = GetModuleHandleW(nullptr);
    WNDCLASSEXW window_class {};
    window_class.cbSize = sizeof(window_class);
    window_class.lpfnWndProc = wait_window_proc;
    window_class.hInstance = instance;
    window_class.hCursor = LoadCursor(nullptr, IDC_ARROW);
    window_class.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_BTNFACE + 1);
    window_class.lpszClassName = L"LatencyTestWait";
    RegisterClassExW(&window_class);

    // Sizes are given at 96 dpi and scaled to the system's
    const UINT dpi = GetDpiForSystem();
    const auto px = [dpi](int value) {
      return MulDiv(value, static_cast<int>(dpi), 96);
    };
    const DWORD style = WS_POPUP | WS_CAPTION | WS_SYSMENU;
    const DWORD ex_style = WS_EX_TOPMOST | WS_EX_DLGMODALFRAME;
    RECT rect {0, 0, px(480), px(170)};
    AdjustWindowRectExForDpi(&rect, style, FALSE, ex_style, dpi);
    const int width = rect.right - rect.left;
    const int height = rect.bottom - rect.top;
    MONITORINFO monitor {};
    monitor.cbSize = sizeof(monitor);
    GetMonitorInfoW(MonitorFromWindow(app.hwnd, MONITOR_DEFAULTTOPRIMARY), &monitor);
    const RECT &work = monitor.rcWork;

    wait_window_t state;
    state.app = &app;
    const HWND hwnd = CreateWindowExW(ex_style, window_class.lpszClassName, L"latency-test: finishing up", style, work.left + (work.right - work.left - width) / 2, work.top + (work.bottom - work.top - height) / 2, width, height, nullptr, nullptr, instance, nullptr);
    if (!hwnd) {
      return;
    }
    SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(&state));

    NONCLIENTMETRICSW metrics {};
    metrics.cbSize = sizeof(metrics);
    SystemParametersInfoForDpi(SPI_GETNONCLIENTMETRICS, sizeof(metrics), &metrics, 0, dpi);
    const HFONT font = CreateFontIndirectW(&metrics.lfMessageFont);

    state.text = CreateWindowExW(0, L"STATIC", L"", WS_CHILD | WS_VISIBLE, px(16), px(14), px(448), px(70), hwnd, nullptr, instance, nullptr);
    state.progress = CreateWindowExW(0, PROGRESS_CLASSW, nullptr, WS_CHILD | WS_VISIBLE, px(16), px(90), px(448), px(18), hwnd, nullptr, instance, nullptr);
    const HWND button = CreateWindowExW(0, L"BUTTON", L"Close anyway", WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON, px(464 - 130), px(124), px(130), px(32), hwnd, reinterpret_cast<HMENU>(static_cast<INT_PTR>(close_anyway_id)), instance, nullptr);
    for (const HWND child : {state.text, button}) {
      SendMessageW(child, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
    }
    SendMessageW(state.progress, PBM_SETRANGE32, 0, 100);

    refresh_wait_window(hwnd, state);
    if (!state.done) {
      ShowWindow(hwnd, SW_SHOW);
      SetForegroundWindow(hwnd);
      SetTimer(hwnd, wait_timer_id, 250, nullptr);
      MSG msg;
      while (!state.done && GetMessageW(&msg, nullptr, 0, 0) > 0) {
        if (!IsDialogMessageW(hwnd, &msg)) {
          TranslateMessage(&msg);
          DispatchMessageW(&msg);
        }
      }
    }
    DeleteObject(font);
  }

  /**
   * @brief Run the test app.
   *
   * @param options Command line options.
   * @return Exit code.
   */
  int run(const latency_test::options_t &options) {
    app_t app;
    app.options = options;

    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    app.qpc_frequency = frequency.QuadPart;
    app.qpc_start = qpc_now();

    const HINSTANCE instance = GetModuleHandleW(nullptr);
    WNDCLASSEXW window_class {};
    window_class.cbSize = sizeof(window_class);
    window_class.lpfnWndProc = window_proc;
    window_class.hInstance = instance;
    window_class.hCursor = LoadCursor(nullptr, IDC_ARROW);
    window_class.lpszClassName = L"LatencyTest";
    RegisterClassExW(&window_class);

    DWORD style;
    RECT rect;
    if (options.windowed) {
      style = WS_OVERLAPPEDWINDOW;
      rect = {0, 0, 1280, 720};
      AdjustWindowRect(&rect, style, FALSE);
      OffsetRect(&rect, 100 - rect.left, 100 - rect.top);
    }
    else {
      // Borderless and covering the whole primary monitor, so DWM can flip it directly to the display.
      style = WS_POPUP;
      MONITORINFO info {};
      info.cbSize = sizeof(info);
      GetMonitorInfoW(MonitorFromPoint({0, 0}, MONITOR_DEFAULTTOPRIMARY), &info);
      rect = info.rcMonitor;
    }

    app.hwnd = CreateWindowExW(0, window_class.lpszClassName, L"Latency Test", style, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, nullptr, nullptr, instance, nullptr);
    if (!app.hwnd) {
      return fail("CreateWindowExW", HRESULT_FROM_WIN32(GetLastError()));
    }
    SetWindowLongPtrW(app.hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(&app));

    if (const int code = init_d3d(app)) {
      return code;
    }

    // Input sink so inputs still count if another window steals the foreground.
    const RAWINPUTDEVICE devices[] = {
      {0x01, 0x06, RIDEV_INPUTSINK, app.hwnd},  // keyboard
      {0x01, 0x02, RIDEV_INPUTSINK, app.hwnd},  // mouse
    };
    if (!RegisterRawInputDevices(devices, std::size(devices), sizeof(RAWINPUTDEVICE))) {
      return fail("RegisterRawInputDevices", HRESULT_FROM_WIN32(GetLastError()));
    }

    ShowWindow(app.hwnd, SW_SHOW);
    SetForegroundWindow(app.hwnd);

    const int refresh = refresh_rate(app.hwnd);
    // Without --fps, follow RTSS's frame rate limit when it sets one, as a game would run under it, else the display
    app.rtss_fps = read_rtss_limit();
    app.status.default_source = app.rtss_fps ? latency_test::fps_source_e::rtss : latency_test::fps_source_e::display;
    app.status.fps_source = app.options.fps > 0 ? latency_test::fps_source_e::option : app.status.default_source;
    if (app.options.fps <= 0) {
      app.options.fps = app.rtss_fps ? *app.rtss_fps : (refresh > 0 ? refresh : 60);
    }
    app.status.fps = app.options.fps;
    app.status.refresh_hz = refresh;
    app.status.vsync = app.options.vsync;
    app.status.tearing = !app.options.vsync && app.tearing;
    app.status.auto_flip = app.options.auto_flip;
    app.status.auto_min_ms = app.options.auto_min_ms;
    app.status.auto_max_ms = app.options.auto_max_ms;
    time_server_t time_server;
    std::string time_server_state = "off";
    if (app.options.time_port > 0) {
      const std::string error = start_time_server(time_server, app.options.time_port, app.qpc_frequency);
      time_server_state = error.empty() ? std::format("port {}", app.options.time_port) : "failed: " + error;
    }
    open_log(app, refresh, time_server_state);

    // Latency Cam uploads its recordings to the same port over TCP, and the analysis runs here
    latency_test::upload_server_t upload_server;
    if (time_server.running) {
      latency_test::analysis_config_t config;
      config.results_dir = app.options.results_dir.empty() ? default_results_dir() : std::filesystem::path(to_utf16(app.options.results_dir));
      config.analyzer = to_utf16(app.options.analyzer_path);
      config.python = to_utf16(app.options.python_path);
      config.host_log = to_utf16(app.log_path);
      config.width = app.width;
      config.height = app.height;
      config.fps = app.options.fps;
      config.flush_logs = [&app]() {
        // The C runtime locks each stream, so this is safe while the render loop writes
        if (app.log) {
          std::fflush(app.log);
        }
        if (app.frames_log) {
          std::fflush(app.frames_log);
        }
      };
      app.results_dir = config.results_dir;
      const std::string upload_error = upload_server.start(app.options.time_port, std::move(config));
      app.status.latency_cam_port = upload_error.empty() ? app.options.time_port : 0;
      if (upload_error.empty()) {
        app.upload_server = &upload_server;
      }
    }

    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
    const HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);

    std::int64_t period = static_cast<std::int64_t>(static_cast<double>(app.qpc_frequency) / latency_test::timer_fps(app.options.fps, app.status.fps_source));
    std::int64_t next_frame = qpc_now();
    app.rng.seed(static_cast<std::uint32_t>(next_frame));
    if (app.options.auto_flip) {
      schedule_auto_flip(app, next_frame);
    }
    while (app.running) {
      MSG msg;
      while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
        if (msg.message == WM_QUIT) {
          app.running = false;
          break;
        }
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
      }
      if (!app.running) {
        break;
      }
      if (app.exit_requested) {
        // Closed during an upload or analysis: the pattern is hidden; wait for it (or Close anyway), then quit
        wait_before_closing(app);
        DestroyWindow(app.hwnd);
        continue;
      }

      if (app.resize_pending) {
        if (const HRESULT hr = resize(app); FAILED(hr)) {
          app.upload_server = nullptr;
          upload_server.stop();
          stop_time_server(time_server);
          return fail("Resizing the swap chain", hr);
        }
      }

      if (app.toggle_vsync) {
        app.toggle_vsync = false;
        set_vsync(app, !app.options.vsync);
      }
      if (app.toggle_auto) {
        app.toggle_auto = false;
        set_auto_flip(app, !app.options.auto_flip, qpc_now());
      }
      if (app.new_fps) {
        set_fps(app, *app.new_fps);
        app.new_fps.reset();
        period = static_cast<std::int64_t>(static_cast<double>(app.qpc_frequency) / latency_test::timer_fps(app.options.fps, app.status.fps_source));
        next_frame = qpc_now();
      }

      // Inputs are answered right away; the background only moves on at the chosen rate. With v-sync at the
      // display's own rate, Present sets the pace instead: it blocks until the display takes the last frame.
      const std::int64_t now = qpc_now();
      const bool paced_by_display = app.options.vsync && app.status.fps_source == latency_test::fps_source_e::display;
      const bool frame_due = paced_by_display || now >= next_frame;
      // An automatic flip waits for the next scheduled frame instead of adding one, so the cadence stays steady
      if (frame_due && app.options.auto_flip && now >= app.next_auto_qpc) {
        queue_input(app, 'a', 0, now);
        schedule_auto_flip(app, now);
      }
      if (!app.pending.empty() || frame_due) {
        render(app);
        if (frame_due) {
          next_frame += period;
          if (next_frame <= now) {
            next_frame = now + period;
          }
        }
        continue;
      }

      LARGE_INTEGER due;
      due.QuadPart = -std::max<std::int64_t>(1, (next_frame - now) * 10'000'000 / app.qpc_frequency);
      SetWaitableTimer(timer, &due, 0, nullptr, nullptr, FALSE);
      MsgWaitForMultipleObjectsEx(1, &timer, INFINITE, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
    }

    CloseHandle(timer);
    app.upload_server = nullptr;
    upload_server.stop();
    stop_time_server(time_server);
    if (app.log) {
      std::fclose(app.log);
    }
    if (app.frames_log) {
      std::fclose(app.frames_log);
    }
    return 0;
  }
}  // namespace

/**
 * @brief Entry point of the test app.
 *
 * @return Exit code.
 */
int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
  SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

  int argc = 0;
  LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
  std::vector<std::string> storage;
  for (int i = 1; i < argc; ++i) {
    storage.push_back(to_utf8(argv[i]));
  }
  LocalFree(argv);
  const std::vector<std::string_view> args(storage.begin(), storage.end());

  std::string error;
  const auto options = latency_test::parse_options(args, error);
  if (!options || options->help) {
    const std::string text = options ? std::string(latency_test::usage) : error + "\n\n" + std::string(latency_test::usage);
    MessageBoxW(nullptr, to_utf16(text).c_str(), L"latency-test", options ? MB_ICONINFORMATION : MB_ICONERROR);
    return options ? 0 : 1;
  }
  return run(*options);
}
