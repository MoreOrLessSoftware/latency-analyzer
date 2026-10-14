/**
 * @file tools/latency_test/main.cpp
 * @brief Host side test app for measuring input to photon latency with a camera.
 *
 * Run it as a Sunshine app and stream it to the client under test. It draws the test pattern described in
 * pattern.h and answers every key press (Esc quits) or mouse button press by presenting a frame with the trigger patch flipped,
 * as soon as the input arrives: it does not wait for the next background frame or, unless --vsync is passed, for
 * vertical blank. Each input and the frame that answered it are logged to a CSV file with QueryPerformanceCounter
 * timestamps, so host side delays can be lined up with Sunshine and client logs.
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define WINVER 0x0A00

// local includes
#include "pattern.h"

// platform includes
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi1_6.h>
#include <shellapi.h>
#include <windows.h>
#include <wrl/client.h>

// standard includes
#include <cstdio>
#include <deque>
#include <format>

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
    char source;  ///< 'k' for a key, 'm' for a mouse button.
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

    std::deque<input_event_t> pending;  ///< Inputs not answered yet.
    std::uint32_t next_seq = 1;  ///< Sequence number for the next input.
    std::uint32_t shown_seq = 0;  ///< Sequence number currently shown.
    std::int64_t shown_input_qpc = 0;  ///< When the input currently shown arrived; 0 before any input.
    std::uint32_t frame = 0;  ///< Frames presented so far.
    bool keys_down[256] {};  ///< Keys currently held, to ignore auto repeat.

    std::FILE *log {};  ///< CSV log.
    std::int64_t qpc_start {};  ///< QueryPerformanceCounter value logged times are relative to.
    std::int64_t qpc_frequency {};  ///< QueryPerformanceCounter ticks per second.
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
      }
      else if (vkey == VK_ESCAPE) {
        // Esc quits instead of counting as an input
        PostMessageW(app.hwnd, WM_CLOSE, 0, 0);
      }
      else if (!app.keys_down[vkey]) {
        app.keys_down[vkey] = true;
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
        // Hide the cursor so Sunshine does not draw it over the pattern.
        if (LOWORD(lparam) == HTCLIENT) {
          SetCursor(nullptr);
          return TRUE;
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

    app.context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    app.context->VSSetShader(app.vs.Get(), nullptr, 0);
    app.context->PSSetShader(app.ps.Get(), nullptr, 0);
    app.context->PSSetConstantBuffers(0, 1, app.constants.GetAddressOf());
    return 0;
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

    app.context->OMSetRenderTargets(1, app.rtv.GetAddressOf(), nullptr);
    app.context->Draw(3, 0);

    const UINT sync_interval = app.options.vsync ? 1 : 0;
    const UINT present_flags = !app.options.vsync && app.tearing ? DXGI_PRESENT_ALLOW_TEARING : 0;
    const std::int64_t present_qpc = qpc_now();
    app.swap_chain->Present(sync_interval, present_flags);
    const std::int64_t presented_qpc = qpc_now();

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
   * @param app App state.
   * @param refresh Display refresh rate.
   */
  void open_log(app_t &app, int refresh) {
    std::wstring path = to_utf16(app.options.log_path);
    if (path.empty()) {
      wchar_t temp[MAX_PATH + 1] {};
      GetTempPathW(MAX_PATH + 1, temp);
      path = std::wstring(temp) + L"sunshine_latency_test.csv";
    }
    app.log = _wfopen(path.c_str(), L"w");
    if (!app.log) {
      return;
    }

    FILETIME file_time;
    GetSystemTimePreciseAsFileTime(&file_time);
    SYSTEMTIME utc;
    FileTimeToSystemTime(&file_time, &utc);
    const auto start_ticks = (static_cast<std::uint64_t>(file_time.dwHighDateTime) << 32) | file_time.dwLowDateTime;

    std::fputs(
      std::format(
        "# sunshine latency-test\n"
        "# start_utc={:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z start_filetime={} start_qpc={} qpc_frequency={}\n"
        "# width={} height={} refresh_hz={} fps={} vsync={} tearing={} noise={}\n"
        "# Times are microseconds of QueryPerformanceCounter since start_qpc.\n"
        "seq,source,detail,input_us,render_us,present_call_us,present_return_us,frame,still_queued\n",
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
        app.options.vsync,
        app.tearing,
        app.options.noise
      )
        .c_str(),
      app.log
    );
    std::fflush(app.log);
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
    window_class.lpszClassName = L"SunshineLatencyTest";
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

    app.hwnd = CreateWindowExW(0, window_class.lpszClassName, L"Sunshine Latency Test", style, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, nullptr, nullptr, instance, nullptr);
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
    if (app.options.fps <= 0) {
      app.options.fps = refresh > 0 ? refresh : 60;
    }
    open_log(app, refresh);

    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
    const HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);

    const auto period = static_cast<std::int64_t>(static_cast<double>(app.qpc_frequency) / app.options.fps);
    std::int64_t next_frame = qpc_now();
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

      if (app.resize_pending) {
        if (const HRESULT hr = resize(app); FAILED(hr)) {
          return fail("Resizing the swap chain", hr);
        }
      }

      // Inputs are answered right away; the background only moves on at the chosen rate.
      const std::int64_t now = qpc_now();
      const bool frame_due = now >= next_frame;
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
    if (app.log) {
      std::fclose(app.log);
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
