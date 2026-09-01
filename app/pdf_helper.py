import fitz  # 对应 pip install pymupdf
import os

def pdf_to_images(pdf_path, output_folder, zoom=3.0):
    """
    将 PDF 文件按页转换为高分辨率图片 (PNG格式)，专为 OCR 优化。
    
    :param pdf_path: PDF 文件的绝对或相对路径
    :param output_folder: 图片保存的临时目录
    :param zoom: 缩放倍数。3.0 大约对应 200+ DPI，对识别数学公式和手写字足够清晰
    :return: 生成的图片路径列表 (List[str])
    """
    # 确保输出的临时文件夹存在
    os.makedirs(output_folder, exist_ok=True)
    
    image_paths = []
    try:
        # 打开 PDF 文档
        pdf_document = fitz.open(pdf_path)
        
        # 提取文件名（不带后缀），用来给图片命名避免冲突
        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
        
        # 逐页转换
        for page_num in range(len(pdf_document)):
            page = pdf_document.load_page(page_num)
            
            # 使用 Matrix 提升分辨率，这对 glm_ocr 识别公式非常关键
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            
            # 拼接输出路径：例如 张三_page_1.png
            image_filename = f"{base_name}_page_{page_num + 1}.png"
            image_path = os.path.join(output_folder, image_filename)
            
            # 保存图片并记录路径
            pix.save(image_path)
            image_paths.append(image_path)
            
        pdf_document.close()
        return image_paths
        
    except Exception as e:
        print(f"Error: 转换 PDF [{pdf_path}] 时出错: {str(e)}")
        # 发生错误时返回空列表，方便 Agent 捕获异常并跳过该学生
        return []

# 测试模块（如果 Claude 尝试直接运行这个脚本，可以走这里测试）
if __name__ == "__main__":
    # 测试用的占位符
    test_pdf = "sample.pdf" 
    test_out = "temp_workspace/images"
    if os.path.exists(test_pdf):
        print("开始测试转换...")
        paths = pdf_to_images(test_pdf, test_out)
        print(f"成功生成 {len(paths)} 张图片:\n", paths)
    else:
        print("当前目录下没有找到 sample.pdf，请在实际流程中调用 pdf_to_images 函数。")