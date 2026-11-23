/**
 * ESLint flat config（首轮策略：发现即报告，既有违规以 warn 呈现，不阻断 CI）。
 *
 * - 先接入 eslint:recommended + vue flat/recommended，逐步收敛；
 * - 与原有 scripts/check_*.mjs 断言互补：那些守行为契约，这里守语法/常见错误；
 * - `npm run lint` 用 --max-warnings 9999：新增错误会失败，存量告警先不卡。
 */
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

export default [
  {
    ignores: ['dist/**', 'node_modules/**', 'dev/**', 'scripts/**'],
  },
  js.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.{js,mjs,vue}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      // 存量代码的规则降噪：先看得到，但不因历史问题阻断
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'no-undef': 'warn',
      'no-empty': 'warn',
      'no-constant-condition': 'warn',
      // 注释里的全角空格是有意示例（如「书名　第二卷」），不视为错误
      'no-irregular-whitespace': ['error', { skipComments: true }],
      // 单文件组件与既有模板风格：关闭纯排版类规则
      'vue/multi-word-component-names': 'off',
      'vue/require-default-prop': 'off',
      'vue/no-v-html': 'off',
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/first-attribute-linebreak': 'off',
      'vue/attributes-order': 'off',
      'vue/require-prop-types': 'warn',
    },
  },
]
