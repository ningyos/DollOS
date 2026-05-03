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
