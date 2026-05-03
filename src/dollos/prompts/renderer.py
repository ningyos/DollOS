"""PromptRenderer — render jinja2 templates from the embedded templates package."""

from jinja2 import Environment, PackageLoader, select_autoescape


class PromptRenderer:
    """Render jinja2 templates from the embedded `dollos.prompts.templates` package.

    Caller passes a template name (without `.jinja` suffix) and ctx kwargs;
    receives back the rendered string. Autoescape is OFF — prompts are plain
    text, not HTML.
    """

    def __init__(self) -> None:
        self._env = Environment(
            loader=PackageLoader("dollos.prompts", "templates"),
            autoescape=select_autoescape(disabled_extensions=("jinja",), default=False),
            keep_trailing_newline=False,
        )

    def render(self, template_name: str, **ctx: object) -> str:
        """Render the named template with ctx vars and return the resulting string.

        template_name must NOT include the `.jinja` suffix; "scaffolding" loads
        "scaffolding.jinja". Raises jinja2.TemplateNotFound if the template
        isn't found in the templates package.
        """
        template = self._env.get_template(f"{template_name}.jinja")
        return template.render(**ctx)

    def render_blocks(self, template_name: str, **ctx: object) -> dict[str, str]:
        """Render every `{% block %}` section in the template, return as dict.

        Each block is rendered with the same ctx; result keyed by block name.
        Per-block trailing/leading whitespace is stripped. Useful when one
        template defines multiple related prompt segments (e.g. system + user)
        that should evolve together.

        Raises jinja2.TemplateNotFound if the template isn't found.
        """
        template = self._env.get_template(f"{template_name}.jinja")
        ctx_obj = template.new_context(ctx)
        return {
            name: "".join(block(ctx_obj)).strip()
            for name, block in template.blocks.items()
        }
