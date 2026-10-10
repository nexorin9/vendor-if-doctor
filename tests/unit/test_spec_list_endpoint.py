"""list_endpoint 字段加载测试（task 6）：验证新增 HttpListEndpoint 在 YAML 加载路径可用。"""

from __future__ import annotations

import os

from vendor_if_doctor.spec import load_vendor_spec


def test_load_vendor_spec_with_list_endpoint(tmp_path) -> None:
    """含 list_endpoint 的 YAML 应正确加载到 HttpListEndpoint。"""
    yaml_path = tmp_path / "spec.yaml"
    yaml_path.write_text(
        """
vendor_spec:
  schema_version: "1.0"
  run_window_minutes: 30
  interfaces:
    - vendor_id: demo
      interface_id: health
      display_name: "Demo Health"
      protocol: http
      host: demo.example.com
      port: 80
      base_path: /
      timeout_seconds: 5
      auth:
        type: bearer
        secret_env: VENDOR_DEMO_TOKEN
      sample_endpoint:
        http:
          method: GET
          path: /health
          list_endpoint:
            method: GET
            path: /items
            page_param: page
            page_size_param: size
            page_size: 25
            expected_count_min: 5
      required_fields:
        - "$.code"
""",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["VENDOR_DEMO_TOKEN"] = "demo-secret"
    spec = load_vendor_spec(str(yaml_path), env=env, validate_credentials=True)
    ifc = spec.interfaces[0]
    assert ifc.sample_endpoint.http is not None
    list_ep = ifc.sample_endpoint.http.list_endpoint
    assert list_ep is not None
    assert list_ep.path == "/items"
    assert list_ep.page_param == "page"
    assert list_ep.page_size_param == "size"
    assert list_ep.page_size == 25
    assert list_ep.expected_count_min == 5


def test_load_vendor_spec_without_list_endpoint_optional(tmp_path) -> None:
    """未配置 list_endpoint 时字段为 None（兼容 task 5 的 spec）。"""
    yaml_path = tmp_path / "spec.yaml"
    yaml_path.write_text(
        """
vendor_spec:
  schema_version: "1.0"
  run_window_minutes: 30
  interfaces:
    - vendor_id: demo
      interface_id: health
      display_name: "Demo Health"
      protocol: http
      host: demo.example.com
      port: 80
      auth:
        type: none
      sample_endpoint:
        http:
          method: GET
          path: /health
      required_fields:
        - "$.code"
""",
        encoding="utf-8",
    )

    spec = load_vendor_spec(str(yaml_path), env={}, validate_credentials=False)
    ifc = spec.interfaces[0]
    list_ep = ifc.sample_endpoint.http.list_endpoint
    assert list_ep is None
