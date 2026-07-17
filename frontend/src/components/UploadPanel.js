import { icon } from "./Icon.js";

export function UploadPanel(root, props) {
  const isDisabled = props.disabled || props.loading;

  root.innerHTML = `
    <section class="upload-workspace" aria-label="合同上传入口">
      <div class="upload-heading">
        <p class="section-kicker">NEW REVIEW</p>
        <h2>新建合同审查</h2>
        <p>选择合同文件和审查立场，系统将创建一条可追踪的审查任务。</p>
      </div>

      <div class="upload-tool">
        <label class="file-drop-field">
          <span class="upload-icon">${icon("upload")}</span>
          <strong>合同文件</strong>
          <span class="file-name" data-file-name>选择 DOCX 或 PDF 文件</span>
          <input id="contract-file" type="file" accept=".docx,.pdf" ${isDisabled ? "disabled" : ""} />
          <span class="button button-secondary file-picker">选择文件</span>
        </label>

        <fieldset class="role-field" ${isDisabled ? "disabled" : ""}>
          <legend>审查立场</legend>
          <div class="segmented-control">
          <label>
            <input type="radio" name="review-position" value="甲方" />
            <span>甲方</span>
          </label>
          <label>
            <input type="radio" name="review-position" value="乙方" />
            <span>乙方</span>
          </label>
          </div>
        </fieldset>

        <div class="upload-actions">
          ${props.canCancel ? '<button class="button button-ghost" type="button" data-cancel-upload>取消</button>' : ""}
          <button class="button button-primary" id="start-review" type="button" disabled>
            ${icon("upload")}<span>上传并解析</span>
          </button>
        </div>
      </div>

      <div class="upload-status" aria-live="polite">
        <p class="form-message" id="upload-message">
          ${props.disabled ? "请先启动后端服务。" : "请选择 DOCX/PDF 合同文件和审查立场。"}
        </p>
        <p>PDF 仅支持可复制文本的文件，扫描件当前未启用 OCR。</p>
        <p class="error-message" id="upload-error" hidden></p>
      </div>
    </section>
  `;

  const fileInput = root.querySelector("#contract-file");
  const roleInputs = Array.from(root.querySelectorAll('input[name="review-position"]'));
  const startButton = root.querySelector("#start-review");
  const message = root.querySelector("#upload-message");
  const errorMessage = root.querySelector("#upload-error");
  const fileName = root.querySelector("[data-file-name]");

  if (props.error) {
    errorMessage.hidden = false;
    errorMessage.textContent = props.error;
  }

  function getSelectedRole() {
    return roleInputs.find((input) => input.checked)?.value || "";
  }

  function updateState() {
    const file = fileInput.files[0];
    const hasFile = Boolean(file);
    const hasRole = Boolean(getSelectedRole());
    const supportedFile = hasFile && /\.(docx|pdf)$/i.test(file.name);
    fileName.textContent = hasFile ? file.name : "选择 DOCX 或 PDF 文件";
    startButton.disabled = isDisabled || !(supportedFile && hasRole);

    if (props.disabled) {
      message.textContent = "请先启动后端服务。";
    } else if (props.loading) {
      message.textContent = "正在上传并解析合同。";
    } else if (hasFile && !supportedFile) {
      message.textContent = "仅支持 .docx 或 .pdf 文件。";
    } else if (supportedFile && hasRole) {
      message.textContent = "入口已就绪，可以上传并解析合同。";
    } else {
      message.textContent = "请选择 DOCX/PDF 合同文件和审查立场。";
    }
  }

  fileInput.addEventListener("change", updateState);
  roleInputs.forEach((input) => input.addEventListener("change", updateState));
  updateState();
  startButton.addEventListener("click", () => {
    props.onSubmit({
      file: fileInput.files[0],
      reviewPosition: getSelectedRole(),
    });
  });
  root.querySelector("[data-cancel-upload]")?.addEventListener("click", props.onCancel);
}
