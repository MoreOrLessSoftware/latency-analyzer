/**
 * @file tests/test_http.cpp
 * @brief Test app/http.h.
 */

// test includes
#include <gtest/gtest.h>

// local includes
#include <app/http.h>

using latency_test::route_e;

TEST(HttpTest, ParsesTheRequestLineAndHeaders) {
  const auto request = latency_test::parse_request_head(
    "PUT /recordings/run_1.mp4?x=1 HTTP/1.1\r\nHost: 10.0.0.29\r\ncontent-length:  1234 \r\nExpect: 100-continue\r\n"
  );
  ASSERT_TRUE(request);
  EXPECT_EQ(request->method, "PUT");
  EXPECT_EQ(request->path, "/recordings/run_1.mp4");
  ASSERT_TRUE(request->content_length);
  EXPECT_EQ(*request->content_length, 1234u);
  EXPECT_TRUE(request->expect_continue);
}

TEST(HttpTest, LeavesOutBadOrMissingLengths) {
  EXPECT_FALSE(latency_test::parse_request_head("GET / HTTP/1.1\r\n")->content_length);
  EXPECT_FALSE(latency_test::parse_request_head("PUT /x HTTP/1.1\r\nContent-Length: 12x\r\n")->content_length);
  EXPECT_FALSE(latency_test::parse_request_head("PUT /x HTTP/1.1\r\nContent-Length: -1\r\n")->content_length);
  EXPECT_FALSE(latency_test::parse_request_head("GET / HTTP/1.1\r\nExpect: something\r\n")->expect_continue);
}

TEST(HttpTest, RejectsMalformedRequestLines) {
  EXPECT_FALSE(latency_test::parse_request_head("GET /\r\n"));
  EXPECT_FALSE(latency_test::parse_request_head("GET / FTP/1.0\r\n"));
  EXPECT_FALSE(latency_test::parse_request_head("GET x HTTP/1.1\r\n"));
  EXPECT_FALSE(latency_test::parse_request_head("garbage"));
}

TEST(HttpTest, RoutesEveryEndpoint) {
  const auto check = [](std::string_view method, std::string_view path, route_e kind, std::string_view name) {
    const auto route = latency_test::route_request(method, path);
    EXPECT_EQ(route.kind, kind) << method << " " << path;
    EXPECT_EQ(route.name, name) << method << " " << path;
  };
  check("GET", "/", route_e::index, "");
  check("PUT", "/recordings/amf-av1_20261101-081459.mp4", route_e::upload_video, "amf-av1_20261101-081459");
  check("PUT", "/recordings/run.camera.json", route_e::upload_timing, "run");
  check("POST", "/recordings/run/analyze", route_e::analyze, "run");
  check("GET", "/recordings/run/status", route_e::status, "run");
  check("GET", "/recordings/run/report", route_e::report, "run");
  check("GET", "/recordings/run.mp4", route_e::not_found, "");
  check("DELETE", "/recordings/run.mp4", route_e::not_found, "");
  check("GET", "/elsewhere", route_e::not_found, "");
}

TEST(HttpTest, RefusesNamesThatLeaveTheFolder) {
  EXPECT_EQ(latency_test::route_request("PUT", "/recordings/../x.mp4").kind, route_e::not_found);
  EXPECT_EQ(latency_test::route_request("PUT", "/recordings/a/b.mp4").kind, route_e::not_found);
  EXPECT_EQ(latency_test::route_request("PUT", "/recordings/a\\b.mp4").kind, route_e::not_found);
  EXPECT_EQ(latency_test::route_request("PUT", "/recordings/.mp4").kind, route_e::not_found);
  EXPECT_EQ(latency_test::route_request("PUT", "/recordings/c:x.mp4").kind, route_e::not_found);
  EXPECT_EQ(latency_test::route_request("GET", "/recordings/a%2fb/status").kind, route_e::not_found);
  EXPECT_FALSE(latency_test::valid_name(std::string(201, 'a')));
  EXPECT_TRUE(latency_test::valid_name(std::string(200, 'a')));
}

TEST(HttpTest, EscapesJson) {
  EXPECT_EQ(latency_test::json_escape("a\"b\\c\nd\te\x01"), "a\\\"b\\\\c\\nd\\te\\u0001");
}

TEST(HttpTest, ComparesIgnoringCase) {
  EXPECT_TRUE(latency_test::iequals("Content-Length", "content-length"));
  EXPECT_FALSE(latency_test::iequals("Content-Length", "Content-Lengths"));
  EXPECT_EQ(latency_test::trim(" \tx y\r"), "x y");
}
