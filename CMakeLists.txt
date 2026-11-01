cmake_minimum_required(VERSION 3.25)

project(ml-latency-analyzer
        DESCRIPTION "Camera based input to photon latency testing for game streaming"
        LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(BUILD_TESTS "Build the unit tests" ON)

set(LATENCY_COMPILE_OPTIONS -Wall -Wno-sign-compare)

if(WIN32)
    add_executable(latency-test WIN32 app/main.cpp app/upload_server.cpp)
    target_link_libraries(latency-test PRIVATE d3d11 d3dcompiler dxgi d2d1 dwrite ws2_32 ole32 uuid)
    target_compile_options(latency-test PRIVATE ${LATENCY_COMPILE_OPTIONS})
    if(MINGW)
        # Link the runtime statically so the exe can be copied to a streaming host on its own.
        target_link_options(latency-test PRIVATE -static)
    endif()
    install(TARGETS latency-test RUNTIME DESTINATION .)

    # The analyzer bundled with Python, numpy and OpenCV into one exe, so hosts need no Python. Not part of the
    # default build because it downloads packages the first time and takes a minute:
    #   ninja -C cmake-build-release latency-analyzer
    add_custom_target(latency-analyzer
            COMMAND powershell -NoProfile -ExecutionPolicy Bypass -File ${CMAKE_CURRENT_SOURCE_DIR}/analyzer/build_exe.ps1
                    -BuildDir ${CMAKE_CURRENT_BINARY_DIR}
            WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
            COMMENT "Bundling the analyzer into latency-analyzer.exe with PyInstaller"
            VERBATIM)
    install(PROGRAMS ${CMAKE_CURRENT_BINARY_DIR}/latency-analyzer.exe DESTINATION . OPTIONAL)
endif()

install(FILES
        analyzer/analyze_video.py
        analyzer/report.py
        analyzer/camera_mode.py
        analyzer/camera_timing.py
        analyzer/time_server.py
        analyzer/analyze.bat
        README.md
        DESTINATION .)

if(BUILD_TESTS)
    include(FetchContent)
    FetchContent_Declare(googletest
            URL https://github.com/google/googletest/archive/refs/tags/v1.15.2.tar.gz
            DOWNLOAD_EXTRACT_TIMESTAMP ON
            FIND_PACKAGE_ARGS NAMES GTest)
    set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
    set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(googletest)

    enable_testing()
    add_executable(test_latency tests/test_pattern.cpp tests/test_clock_sync.cpp tests/test_http.cpp)
    target_include_directories(test_latency PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
    target_link_libraries(test_latency PRIVATE GTest::gtest_main)
    target_compile_options(test_latency PRIVATE ${LATENCY_COMPILE_OPTIONS})
    if(MINGW)
        target_link_options(test_latency PRIVATE -static)
    endif()
    include(GoogleTest)
    gtest_discover_tests(test_latency)
endif()
