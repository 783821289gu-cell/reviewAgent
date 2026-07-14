export function UploadPanel(root, props) {
  const isDisabled = props.disabled || props.loading;

  root.innerHTML = `
    <section class="panel upload-panel" aria-label="合同上传入口">
      <div class="panel-header">
        <h2>开始审查</h2>
        <p>上传 NDA 合同并选择审查立场。未选择立场时不能开始审查。</p>
      </div>

      <div class="form-grid">
        <label class="field">
          <span>合同文件</span>
          <input id="contract-file" type="file" accept=".docx,.pdf" ${isDisabled ? "disabled" : ""} />
        </label>

        <fieldset class="field role-field" ${isDisabled ? "disabled" : ""}>
          <legend>审查立场</legend>
          <label>
            <input type="radio" name="review-position" value="甲方" />
            甲方
          </label>
          <label>
            <input type="radio" name="review-position" value="乙方" />
            乙方
          </label>
        </fieldset>

        <button id="start-review" type="button" disabled>上传并解析</button>
      </div>

      <p class="form-message" id="upload-message">
        ${props.disabled ? "请先启动后端服务。" : "请选择 DOCX/PDF 合同文件和审查立场。"}
      </p>
      <p class="error-message" id="upload-error" hidden></p>
    </section>
  `;

  const fileInput = root.querySelector("#contract-file");
  const roleInputs = Array.from(root.querySelectorAll('input[name="review-position"]'));
  const startButton = root.querySelector("#start-review");
  const message = root.querySelector("#upload-message");
  const errorMessage = root.querySelector("#upload-error");

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
    startButton.disabled = isDisabled || !(supportedFile && hasRole);

    if (props.loading) {
      message.textContent = "正在上传并解析合同。";
    } else if (hasFile && !supportedFile) {
      message.textContent = "仅支持 .docx 或 .pdf 文件。";
    } else if (supportedFile && hasRole) {
      message.textContent = "入口已就绪。任务 2 会解析合同并生成条款结构。";
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
}
