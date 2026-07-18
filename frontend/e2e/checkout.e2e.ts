import { expect, test } from "@playwright/test";

const checkoutToken = "checkout-token-publico";
const rawEmail = "marina.silva@cliente-exemplo.com";
const rawDocument = "12345678909";
const externalOrderId = "pedido_externo_987654";

const publicCheckout = {
  agreement: {
    amount: "50000.05",
    currency: "BRL",
    customer: {
      document_masked: "***.456.789-**",
      email_masked: "ma••••@cliente-exemplo.com",
      name: "Marina Silva",
    },
    delivery_due_at: null,
    delivery_window_days: 7,
    fee_bps: 250,
    id: "agr_publico_7Nf93",
    status: "AWAITING_PAYMENT",
  },
};

test("abre checkout sem conta com contrato público mascarado", async ({ page, baseURL }) => {
  const checkoutURL = new URL(`/checkout/${checkoutToken}`, baseURL);

  await page.context().addCookies([
    {
      name: "escrow_session",
      url: checkoutURL.toString(),
      value: "sessao-que-nao-pode-vazar",
    },
  ]);

  await page.route(`**/api/v1/checkout/${checkoutToken}/`, async (route) => {
    const request = route.request();
    const payload = publicCheckout.agreement;

    expect(request.method()).toBe("GET");
    expect(request.headers().authorization).toBeUndefined();
    expect(request.headers().cookie).toBeUndefined();
    expect(request.url()).not.toContain(externalOrderId);
    expect(payload.amount).toMatch(/^\d+\.\d{2}$/);
    expect(payload.customer).toEqual({
      document_masked: expect.stringMatching(/^\*{3}\.\d{3}\.\d{3}-\*{2}$/),
      email_masked: expect.stringMatching(/^[^@]+@[^@]+$/),
      name: "Marina Silva",
    });
    expect(payload.customer).not.toHaveProperty("email");
    expect(payload.customer).not.toHaveProperty("document");
    expect(payload).not.toHaveProperty("external_id");
    expect(JSON.stringify(publicCheckout)).not.toContain(rawEmail);
    expect(JSON.stringify(publicCheckout)).not.toContain(rawDocument);
    expect(JSON.stringify(publicCheckout)).not.toContain(externalOrderId);

    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(publicCheckout),
    });
  });

  await page.goto(checkoutURL.toString());

  await expect(page.getByRole("heading", { name: "Revise seu pagamento" })).toBeVisible();
  await expect(page.getByText(/R\$\s*50\.000,05/)).toBeVisible();
  await expect(page.getByText("ma••••@cliente-exemplo.com")).toBeVisible();
  await expect(page.getByText("***.456.789-**")).toBeVisible();
  await expect(page.locator("body")).not.toContainText(rawEmail);
  await expect(page.locator("body")).not.toContainText(rawDocument);
  await expect(page.locator("body")).not.toContainText(externalOrderId);
});
