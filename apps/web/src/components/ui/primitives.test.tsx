import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiFailure, toFailure } from "../../api/client";
import { Button } from "./primitives";

describe("Button loading state", () => {
  it("stays focusable while busy and ignores activation (focus is never dropped to <body>)", async () => {
    const onClick = vi.fn();
    const { rerender } = render(<Button onClick={onClick}>Run</Button>);
    const button = screen.getByRole("button", { name: "Run" });
    button.focus();
    rerender(
      <Button onClick={onClick} loading>
        Run
      </Button>,
    );
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveFocus();
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("does not submit its form while busy", async () => {
    const onSubmit = vi.fn((e: { preventDefault: () => void }) => e.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <Button type="submit" loading>
          Save
        </Button>
      </form>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("toFailure", () => {
  it("explains a non-JSON gateway failure without an HTTP status code", () => {
    const failure = toFailure(undefined, new Response("<html>Bad Gateway</html>", { status: 502 }));
    expect(failure).toBeInstanceOf(ApiFailure);
    expect(failure.status).toBe(502);
    expect(failure.message).not.toMatch(/\d{3}/);
    expect(failure.message).toMatch(/could not complete the request/);
  });
});
