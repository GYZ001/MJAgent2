import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import JsonViewer from "./JsonViewer";

describe("JsonViewer", () => {
  it("链路详情可让所有嵌套对象默认展开", () => {
    const html = renderToStaticMarkup(createElement(JsonViewer, {
      data: { first: { second: { third: [{ value: 1 }] } } },
      expandAll: true,
    }));

    expect(html.match(/aria-expanded="true"/g)?.length).toBe(6);
    expect(html).toContain("&quot;value&quot;");
  });
});
