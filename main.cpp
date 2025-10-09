#include "clang/Frontend/FrontendActions.h"
#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Tooling.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Lex/Preprocessor.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/raw_ostream.h"

using namespace clang;
using namespace clang::tooling;

// Our callback to track all includes
class IncludeTracker : public PPCallbacks {
public:
    explicit IncludeTracker(SourceManager &SM) : SM(SM) {}

    void InclusionDirective(SourceLocation HashLoc, const Token& IncludeTok, StringRef FileName, bool IsAngled, CharSourceRange FilenameRange, OptionalFileEntryRef File, StringRef SearchPath, StringRef RelativePath, const Module* SuggestedModule, bool ModuleImported, SrcMgr::CharacteristicKind FileType) override {
        using namespace llvm;

        if (!File) {
            outs() << "Error\n";
            return;
        }

        // Get the original path (may be relative)
        SmallString<256> FullPath = File->getName();
        SmallString<256> IncluderFullPath = SM.getFilename(HashLoc);

        // Make it absolute
        if (sys::fs::make_absolute(FullPath) || sys::fs::make_absolute(IncluderFullPath)) {
            outs() << "Error\n";
            return;
        }

        outs() << "# " << FullPath << " | " << IncluderFullPath << "\n";
    }

private:
    SourceManager &SM;
};

class MyFrontendAction : public PreprocessOnlyAction {
protected:
    void ExecuteAction() override {
        Preprocessor &PP = getCompilerInstance().getPreprocessor();
        PP.addPPCallbacks(std::make_unique<IncludeTracker>(PP.getSourceManager()));
        PP.EnterMainSourceFile();
        Token Tok;
        do {
            PP.Lex(Tok);
        } while (Tok.isNot(tok::eof));
    }
};

static llvm::cl::OptionCategory ToolCategory("include-tracker");

int main(int argc, const char **argv) {
    auto ExpectedParser = CommonOptionsParser::create(argc, argv, ToolCategory);
    if (!ExpectedParser) {
        llvm::errs() << ExpectedParser.takeError();
        return 1;
    }
    CommonOptionsParser &OptionsParser = ExpectedParser.get();
    ClangTool Tool(OptionsParser.getCompilations(), OptionsParser.getSourcePathList());
    return Tool.run(newFrontendActionFactory<MyFrontendAction>().get());
}
